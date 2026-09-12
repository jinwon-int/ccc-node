#!/usr/bin/env python3
"""Pre-merge pull-request readiness JSON CLI — same aggregation as the
``pr_readiness`` MCP tool (#1694 item 3).

    ccc-pr-readiness.py --repo OWNER/REPO --pr NUMBER

``stdout`` carries only the JSON result (head/mergeability, CI rollup with
the gh-pr-flow relay counting rule, latest reviews with head matching,
unresolved review threads, and the informational readiness snapshot);
structured errors are JSON on stdout with exit 1; diagnostics go to
``stderr``.  Read-only — nothing here approves, merges, or mutates anything;
results never substitute for approvals or merge-time re-verification.
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
    from telegram_bot.core.pr_readiness import PrReadinessError, collect  # noqa: E402
except ImportError:  # pragma: no cover - worktree aliasing only
    from bridge.core.pr_readiness import PrReadinessError, collect  # noqa: E402


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc-pr-readiness",
        description="Aggregated pre-merge PR readiness (head/CI/reviews/threads) as JSON.",
    )
    parser.add_argument("--repo", required=True, metavar="OWNER/REPO", help="GitHub repository")
    parser.add_argument("--pr", required=True, type=int, metavar="NUMBER", help="pull request number")
    arguments = parser.parse_args(argv)
    try:
        _emit(collect(arguments.repo, arguments.pr))
        return 0
    except PrReadinessError as error:
        _emit(error.payload())
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
