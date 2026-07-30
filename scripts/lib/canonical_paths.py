#!/usr/bin/env python3
"""Canonical harness path rewrite — one implementation for install and diagnosis.

setup.sh installs the repo templates verbatim and then rewrites the INSTALLED
copies so the canonical pair (repo ``/opt/ccc-node``, harness dir
``/root/.claude``) resolves to this node's real checkout and harness directory.
Nodes not installed at that pair therefore hold installed files that legitimately
differ from the templates they came from.

ccc_doctor.py compares installed files against those same templates, so it must
apply the identical transform before comparing. When it did not (byte-exact
``filecmp`` only), every non-canonical node reported permanent phantom drift and
the repair doctor recommended (``--fix --apply --scope=files``) copied the
canonical template back over a correct file — pointing it at a checkout that does
not exist there (``/opt/ccc-node/bridge/venv/bin/python`` on a ``/root/ccc-node``
install). That was the 2026-07-30 fleet sweep: yukson, gwakga, gongmyoung,
gongyung and daegyo all reported ``교정가능`` for hooks/distill.sh and
hooks/lifecycle-feed.sh while being correctly installed, and following the
printed action would have broken the lifecycle feed hook on all five.

Both callers use this module so the install-time and diagnosis-time transforms
cannot drift apart again.

This file is deliberately NOT installed into the harness directory: it carries
the canonical literals, so an installed copy would have its own constants
rewritten by the very transform it defines.
"""

from __future__ import annotations

import sys
from pathlib import Path
import re

CANONICAL_REPO = "/opt/ccc-node"
CANONICAL_CLAUDE_DIR = "/root/.claude"


def rewrite_pairs(repo: str | Path, claude_dir: str | Path) -> dict[str, str]:
    """Canonical -> actual substitutions that are not identity on this node.

    An empty result means the node is installed at the canonical pair and
    installed files are byte-identical copies of the templates.
    """
    wanted = (
        (CANONICAL_REPO, str(repo)),
        (CANONICAL_CLAUDE_DIR, str(claude_dir)),
    )
    return {old: new for old, new in wanted if old != new}


def rewrite_text(text: str, pairs: dict[str, str]) -> str:
    """Apply ``pairs`` in a single non-cascading pass.

    Every occurrence in the ORIGINAL text is replaced exactly once and
    replacement values are never rescanned, so one pair's output cannot be
    corrupted by the other pair — e.g. a checkout under a path containing
    ``/root/.claude`` cannot have its freshly inserted repo path corrupted by
    the harness-dir pair. Longest token first for deterministic behavior on
    overlapping prefixes.
    """
    if not pairs:
        return text
    pattern = re.compile(
        "|".join(re.escape(tok) for tok in sorted(pairs, key=len, reverse=True))
    )
    return pattern.sub(lambda m: pairs[m.group(0)], text)


def rewrite_file(path: str | Path, pairs: dict[str, str]) -> bool:
    """Rewrite an installed file in place. True when its content changed."""
    if not pairs:
        return False
    target = Path(path)
    original = target.read_text(encoding="utf-8")
    updated = rewrite_text(original, pairs)
    if updated == original:
        return False
    target.write_text(updated, encoding="utf-8")
    return True


def main(argv: list[str]) -> int:
    # <file> <old> <new> [<old> <new> ...] — the shape setup.sh calls with.
    if len(argv) < 3 or len(argv) % 2 == 0:
        print(
            "usage: canonical_paths.py <file> <old> <new> [<old> <new> ...]",
            file=sys.stderr,
        )
        return 2
    path, rest = argv[0], argv[1:]
    pairs = {old: new for old, new in zip(rest[0::2], rest[1::2]) if old != new}
    rewrite_file(path, pairs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
