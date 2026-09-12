#!/usr/bin/env python3
"""Pre-deployment diff JSON CLI — same aggregation as the deployment_diff MCP
tool (#1694 item 4).

    ccc-deployment-diff.py

``stdout`` carries only the JSON result (serving checkout, target, history,
installed marker + doctor drift, dependency changes, recovery snapshot, and
the informational deployment verdict); structured errors are JSON on stdout
with exit 1; diagnostics go to ``stderr``.  Read-only — nothing here pulls,
installs, restarts, or approves anything; results never substitute for the
self-update approval flow or a live restart preflight.
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
    from telegram_bot.core.deployment_diff import DeploymentDiffError, collect  # noqa: E402
except ImportError:  # pragma: no cover - worktree aliasing only
    from bridge.core.deployment_diff import DeploymentDiffError, collect  # noqa: E402


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc-deployment-diff",
        description="Aggregated pre-deployment diff (checkout/target/installed/deps/recovery) as JSON.",
    )
    parser.parse_args(argv)
    try:
        _emit(collect())
        return 0
    except DeploymentDiffError as error:
        _emit(error.payload())
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
