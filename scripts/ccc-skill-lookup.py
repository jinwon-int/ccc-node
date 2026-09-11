#!/usr/bin/env python3
"""Skill lookup JSON CLI — same resolution logic as the family-skills MCP.

Subcommands mirror the ``skill_search`` / ``skill_read`` MCP tools
one-to-one (#1678):

    ccc-skill-lookup.py search --query "git pr flow" [--runtime repo] [--limit 10]
    ccc-skill-lookup.py read --id repo:gh-pr-flow [--revision <64-hex>]

``stdout`` carries only the JSON result (success payload, or a structured
``{"error": ...}`` object mirroring the MCP ``isError`` result); diagnostics
go to ``stderr``.  Exit codes: 0 success, 1 structured error.  Node policy
(``CCC_NODE_ISOLATION_PROFILE`` / ``CCC_MEMORY_AUDIENCE``) is enforced from
this process environment exactly as the MCP server does.

Danso and other bash-capable runtimes call this CLI for skill lookup; native
MCP connections are a separate, later work item and must not be assumed.
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
    from telegram_bot.core.skill_lookup import (  # noqa: E402
        DEFAULT_LIMIT,
        MAX_LIMIT,
        RUNTIMES,
        SkillLookupError,
        read,
        search,
    )
except ImportError:  # pragma: no cover - worktree aliasing only
    from bridge.core.skill_lookup import (  # noqa: E402
        DEFAULT_LIMIT,
        MAX_LIMIT,
        RUNTIMES,
        SkillLookupError,
        read,
        search,
    )


def _emit(payload: dict) -> int:
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc-skill-lookup",
        description="Search and read approved node/fleet skills as JSON.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="search skills by name/description")
    search_parser.add_argument("--query", required=True, help="space-separated name/description tokens")
    search_parser.add_argument("--runtime", choices=RUNTIMES, help="origin filter")
    search_parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help=f"max results, 1..{MAX_LIMIT} (default {DEFAULT_LIMIT})"
    )

    read_parser = subparsers.add_parser("read", help="read one validated SKILL.md")
    read_parser.add_argument("--id", dest="skill_id", required=True, help="exact '<runtime>:<name>' id")
    read_parser.add_argument("--revision", help="expected 64-hex revision from the search result")

    args = parser.parse_args(argv)
    try:
        if args.command == "search":
            return _emit(search(args.query, args.runtime, args.limit))
        return _emit(read(args.skill_id, args.revision))
    except SkillLookupError as error:
        # Structured error mirrors the MCP isError result; still stdout-only.
        _emit(error.payload())
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
