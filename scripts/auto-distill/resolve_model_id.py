#!/usr/bin/env python3
"""Derive a receipt-ready ``evaluation.model_resolution`` fragment (#1521).

The auto-distill launcher passes an alias (``--model haiku``) and the provider
resolves it server-side, so two evaluations weeks apart may have run on
different models with identical logs (#1514). This tool recovers the concrete
id from the Claude ``--output-format json`` envelopes an evaluation already
wrote (``modelUsage`` keys) and prints the JSON object to paste into the
receipt. It never calls a model; when no envelope is available the operator
records the object by hand with ``resolved_by`` = ``operator``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from model_command import (
    BARE_MODEL_ALIASES,
    CLAUDE_ARGS,
    claude_model_alias,
    resolved_model_ids,
)


MAX_ENVELOPE_BYTES = 16 * 1024 * 1024


def _read_envelope(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"envelope is missing or unsafe: {path.name}")
    if path.stat().st_size > MAX_ENVELOPE_BYTES:
        raise ValueError(f"envelope exceeds {MAX_ENVELOPE_BYTES} bytes: {path.name}")
    return path.read_text(encoding="utf-8", errors="replace")


def build_resolution(
    alias: str, envelopes: list[Path], *, now: datetime | None = None
) -> dict[str, str]:
    ids: set[str] = set()
    for path in envelopes:
        ids.update(resolved_model_ids(_read_envelope(path)))
    if not ids:
        raise ValueError(
            "no modelUsage id found in the given envelopes; record "
            "model_resolution manually with resolved_by=operator"
        )
    if len(ids) > 1:
        raise ValueError(
            "ambiguous: envelopes report several model ids " + ", ".join(sorted(ids))
        )
    resolved_id = ids.pop()
    if resolved_id.strip().lower() in BARE_MODEL_ALIASES:
        raise ValueError(f"envelope reports a bare alias, not a model id: {resolved_id}")
    resolved_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "alias": alias,
        "resolved_id": resolved_id,
        "resolved_by": "claude-json-modelUsage:"
        + ",".join(path.name for path in envelopes),
        "resolved_at": resolved_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--alias",
        default=claude_model_alias(CLAUDE_ARGS),
        help="alias the evaluation launcher passed (default: the managed CLAUDE_ARGS alias)",
    )
    parser.add_argument(
        "--envelope",
        type=Path,
        action="append",
        default=[],
        help="Claude --output-format json envelope or a log with one envelope per line",
    )
    args = parser.parse_args(argv)
    if not args.alias:
        print("resolve-model-id: no alias configured", file=sys.stderr)
        return 2
    if not args.envelope:
        print(
            "resolve-model-id: alias=%s; the CLI exposes no offline alias table "
            "(claude --version prints only the CLI version). Pass --envelope with "
            "the evaluation's JSON output, or record model_resolution manually "
            "with resolved_by=operator." % args.alias,
            file=sys.stderr,
        )
        return 2
    try:
        resolution = build_resolution(args.alias, args.envelope)
    except (OSError, ValueError) as exc:
        print(f"resolve-model-id: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(resolution, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
