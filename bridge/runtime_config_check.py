#!/usr/bin/env python3
"""Body-free, standard-library-only bridge runtime configuration preflight.

This module intentionally avoids importing the bridge package or third-party
dependencies so ``ccc-doctor`` and ``ccc-self-update`` can run it before the
Telegram application is constructed. It reads only the named timeout settings
and never emits environment values or file contents.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PROCESS_TIMEOUT_SECONDS = 21600
DEFAULT_DELEGATED_TASK_STALL_SECONDS = 7200.0

# Claude Agent SDK stdout NDJSON reader buffer (bytes).
#
# The SDK reads the CLI's stdout as newline-delimited JSON and refuses any
# single line longer than ``ClaudeAgentOptions.max_buffer_size``; when that
# option is left None it falls back to the SDK's own
# ``_DEFAULT_MAX_BUFFER_SIZE`` of 1 MiB
# (claude_agent_sdk/_internal/transport/subprocess_cli.py). Exceeding it raises
# ``SDKJSONDecodeError`` inside the message reader task, which kills the reader
# with no recovery path — the whole turn dies with
# "JSON message exceeded maximum buffer size of 1048576 bytes".
#
# Measured incident (2026-08-03 18:19:14 KST, this node): a single line of
# 1,056,854 bytes — only 8,278 bytes over the 1 MiB limit — killed the bridge
# turn. A 510 KB PNG screenshot went through Read; Claude Code resized and
# re-encoded it to 682x2000 (528,000 base64 chars) and then shipped that same
# base64 TWICE in one message: once as
# ``message.content[0].content[0].source.data`` and again as
# ``toolUseResult.file.base64``. The duplication is what doubles the payload,
# so the effective failure threshold is only ~524 KB of base64 for a SINGLE
# image — routinely reached by ordinary screenshot work.
#
# 16 MiB leaves ~16x headroom over that measured line (and ~8 images' worth of
# the duplicated encoding) at no steady-state cost: this bounds a buffer, it
# does not preallocate one. The bridge must always pass an explicit value so a
# turn can never silently fall back to the 1 MiB SDK default again.
#
# Single-sourced here (this module is stdlib-only and imports nothing from the
# bridge package) so utils/config.py and core/claude_runtime.py cannot drift.
DEFAULT_CLAUDE_MAX_BUFFER_SIZE = 16 * 1024 * 1024  # 16 MiB
# Floor: the SDK's own 1 MiB default. Configuring anything lower is strictly
# worse than the behaviour this fix replaces, so it is rejected.
MIN_CLAUDE_MAX_BUFFER_SIZE = 1024 * 1024
MAX_CLAUDE_MAX_BUFFER_SIZE = 256 * 1024 * 1024
TIMEOUT_ORDER_ERROR = "delegated-task-stall-not-lower-than-process-timeout"
TIMEOUT_ORDER_MESSAGE = (
    "CCC_DELEGATED_TASK_STALL_SECONDS must be lower than CLAUDE_PROCESS_TIMEOUT"
)


class RuntimeConfigInvariantError(ValueError):
    """A body-free runtime configuration failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RuntimeConfigCheck:
    ok: bool
    code: str

    def as_json(self) -> str:
        return json.dumps({"code": self.code, "ok": self.ok}, sort_keys=True)


def validate_timeout_invariant(
    process_timeout_seconds: int,
    delegated_task_stall_seconds: float,
) -> None:
    """Require every delegated task deadline to fit inside its provider turn."""

    if delegated_task_stall_seconds >= process_timeout_seconds:
        raise RuntimeConfigInvariantError(TIMEOUT_ORDER_ERROR, TIMEOUT_ORDER_MESSAGE)


def _read_env_assignment(path: Path, key: str) -> str | None:
    """Read the last assignment using the same bounded syntax as start.sh."""

    if not path.is_file():
        return None
    assignment = re.compile(
        rf"^[ \t]*(?:export[ \t]+)?{re.escape(key)}[ \t]*=(.*)$"
    )
    value: str | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        match = assignment.match(line)
        if match is not None:
            value = match.group(1)
    if value is None:
        return None
    value = value.strip()
    if value.endswith('"'):
        value = value[:-1]
    if value.startswith('"'):
        value = value[1:]
    if value.endswith("'"):
        value = value[:-1]
    if value.startswith("'"):
        value = value[1:]
    return value.split(" #", 1)[0].rstrip()


def _effective_value(
    key: str,
    default: str,
    *,
    environ: Mapping[str, str],
    project_env: Path,
    bridge_env: Path,
) -> str:
    if key in environ:
        return environ[key]
    project_value = _read_env_assignment(project_env, key)
    if project_value is not None:
        return project_value
    bridge_value = _read_env_assignment(bridge_env, key)
    return default if bridge_value is None else bridge_value


def _positive_int(value: str, code: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigInvariantError(code, code) from exc
    if parsed <= 0:
        raise RuntimeConfigInvariantError(code, code)
    return parsed


def _positive_float(value: str, code: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigInvariantError(code, code) from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise RuntimeConfigInvariantError(code, code)
    return parsed


def check_runtime_config(
    *,
    project_root: Path,
    bridge_env: Path,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfigCheck:
    """Validate effective timeout settings without exposing their values."""

    process_values = os.environ if environ is None else environ
    project_env = project_root / ".telegram_bot" / ".env"
    try:
        process_timeout = _positive_int(
            _effective_value(
                "CLAUDE_PROCESS_TIMEOUT",
                str(DEFAULT_PROCESS_TIMEOUT_SECONDS),
                environ=process_values,
                project_env=project_env,
                bridge_env=bridge_env,
            ),
            "invalid-claude-process-timeout",
        )
        delegated_timeout = _positive_float(
            _effective_value(
                "CCC_DELEGATED_TASK_STALL_SECONDS",
                str(DEFAULT_DELEGATED_TASK_STALL_SECONDS),
                environ=process_values,
                project_env=project_env,
                bridge_env=bridge_env,
            ),
            "invalid-delegated-task-stall-timeout",
        )
        validate_timeout_invariant(process_timeout, delegated_timeout)
    except RuntimeConfigInvariantError as exc:
        return RuntimeConfigCheck(ok=False, code=exc.code)
    return RuntimeConfigCheck(ok=True, code="ok")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument(
        "--bridge-env",
        type=Path,
        default=Path(__file__).resolve().parent / ".env",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    result = check_runtime_config(
        project_root=args.project_root.expanduser().resolve(),
        bridge_env=args.bridge_env.expanduser().resolve(),
    )
    if args.json:
        print(result.as_json())
    else:
        print("bridge runtime config: ok" if result.ok else f"bridge runtime config: {result.code}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
