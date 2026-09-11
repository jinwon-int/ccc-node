#!/usr/bin/env python3
"""Task recovery status JSON CLI — same aggregation as the task_status MCP
tool (#1694 item 2).

    ccc-task-status.py

``stdout`` carries only the JSON result (checkpoint, resume note, external
wait promises); structured errors are JSON on stdout with exit 1; diagnostics
go to ``stderr``.  Read-only — nothing here resumes, mutates, or approves
anything; results inform resumption and never substitute for approvals.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CORE_DIR = _REPO_ROOT / "bridge" / "core"
for _path in (str(_REPO_ROOT), str(_CORE_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    from telegram_bot.core.task_status import TaskStatusError, collect  # noqa: E402
except ImportError:  # pragma: no cover - worktree aliasing only
    from bridge.core.task_status import TaskStatusError, collect  # noqa: E402


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc-task-status",
        description="Aggregated task recovery status (checkpoint/resume/waits) as JSON.",
    )
    parser.parse_args(argv)
    try:
        _emit(collect())
        return 0
    except TaskStatusError as error:
        _emit(error.payload())
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
