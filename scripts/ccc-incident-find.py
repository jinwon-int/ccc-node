#!/usr/bin/env python3
"""Incident evidence search JSON CLI — same combination as the incident_find
MCP tool (#1694 item 5).

    ccc-incident-find.py --query "symptom keywords" [--repo OWNER/REPO]

``stdout`` carries only the JSON result (wiki candidates from wiki-agent,
matched issues and PRs from authenticated gh); structured errors are JSON on
stdout with exit 1; diagnostics go to ``stderr``.  Results are candidates
that inform operators — reading the underlying evidence (and any approval)
stays a separate, human step.
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
    from telegram_bot.core.incident_find import IncidentFindError, collect  # noqa: E402
except ImportError:  # pragma: no cover - worktree aliasing only
    from bridge.core.incident_find import IncidentFindError, collect  # noqa: E402


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc-incident-find",
        description="Combined wiki + GitHub incident evidence search as JSON.",
    )
    parser.add_argument("--query", required=True, metavar="TEXT", help="symptom/error keywords")
    parser.add_argument("--repo", default=None, metavar="OWNER/REPO", help="GitHub repository scope (optional)")
    arguments = parser.parse_args(argv)
    try:
        _emit(collect(arguments.query, arguments.repo))
        return 0
    except IncidentFindError as error:
        _emit(error.payload())
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
