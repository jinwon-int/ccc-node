#!/usr/bin/env python3
"""Node status JSON CLI — same aggregation as the family-ops MCP tool (#1694).

    ccc-node-status.py                       # this node
    ccc-node-status.py --node nosuk          # one peer over ssh
    ccc-node-status.py --node a --node b     # aggregate several peers

``stdout`` carries only the JSON result; structured errors are JSON on stdout
with exit 1; diagnostics go to ``stderr``.  Read-only: nothing here restarts,
updates or mutates a node, and results never substitute for approvals.
Remote peers need this repo's ``scripts/ccc-node-status.py`` (path override:
``CCC_NODE_STATUS_REMOTE_PATH``).
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
    from telegram_bot.core.node_status import NodeStatusError, node_status  # noqa: E402
except ImportError:  # pragma: no cover - worktree aliasing only
    from bridge.core.node_status import NodeStatusError, node_status  # noqa: E402


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc-node-status",
        description="Aggregated read-only node status as JSON (local or over ssh).",
    )
    parser.add_argument(
        "--node",
        action="append",
        default=None,
        dest="nodes",
        metavar="SSH_ALIAS",
        help="peer node ssh alias; repeat to aggregate (omit for this node)",
    )
    args = parser.parse_args(argv)
    try:
        _emit(node_status(args.nodes))
        return 0
    except NodeStatusError as error:
        _emit(error.payload())
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
