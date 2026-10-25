#!/usr/bin/env python3
"""Exact native/build-tool lock subsets: the Termux wheel-reuse build input (#2524).

``termux_prepare.py`` builds the native wheels only from two subsets of the
canonical pip-compile locks: the exact ``name==version`` pins plus every
``--hash=sha256:`` entry of the NATIVE packages (``bridge/requirements.lock.txt``)
and of the BUILD_TOOLS packages (``.github/requirements/bridge-ci.txt``). A
pure-Python bump elsewhere in either lock leaves both subsets byte-identical,
so the subset text (not the whole lock file) is what wheel reuse must match.

Standard library only and importable on its own, so that
``termux-prepare-generation.sh`` can pipe the TARGET commit's copy into
``python3 -I -B - --compare-git <repo> <rev-a> <rev-b>`` and decide, before
any worktree exists, whether the serving generation's wheels are candidates.
That decision is only a prefilter; ``termux_prepare.py`` re-checks the subset
hashes against the prior receipt and refuses a mismatch itself.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

NATIVE = ("cryptography", "jiter", "pydantic-core", "rpds-py", "pyromark")
BUILD_TOOLS = ("setuptools", "packaging", "cffi", "pycparser")
MAX_LOCK_BYTES = 4 * 1024**2
# (receipt key, path relative to the repository root, packages). The receipt
# key is also the job's subset file name: <job>/<key>.lock.txt.
LOCK_SOURCES = (
    ("build-tools", ".github/requirements/bridge-ci.txt", BUILD_TOOLS),
    ("native", "bridge/requirements.lock.txt", NATIVE),
)
_RECORD = re.compile(r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s]+)"
                     r"((?:\s+--hash=sha256:[0-9a-f]{64})+)(?=\s|$)")
_REV = re.compile(r"[0-9a-f]{40}")


class LockSubsetError(ValueError):
    """Categorical, body-free lock failure (the message is the reason)."""


def subset_text(text: str, names: tuple[str, ...]) -> str:
    """Copy exact pins/hashes of ``names`` from a canonical lock, fail closed."""
    logical = " ".join(line.strip() for line in text.splitlines()
                       if line.strip() and not line.lstrip().startswith("#"))
    # Backslash-newline continuations are now spaces. Match complete records,
    # rejecting directives, URLs, unpinned requirements and unhashed entries.
    logical = logical.replace("\\", " ")
    found = {}
    position = 0
    while position < len(logical):
        match = _RECORD.match(logical, position)
        if match is None:
            raise LockSubsetError("unsupported_lock_syntax")
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in found:
            raise LockSubsetError("duplicate_lock_requirement")
        found[name] = match[0]
        position = match.end()
        while position < len(logical) and logical[position].isspace():
            position += 1
    if not set(names) <= found.keys():
        raise LockSubsetError("missing_locked_build_requirement")
    return "\n".join(found[name] for name in names) + "\n"


def lock_subset(path: Path, names: tuple[str, ...]) -> str:
    """Subset of a regular, bounded, non-symlink lock file."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LOCK_BYTES:
        raise LockSubsetError("invalid_lock_file")
    return subset_text(path.read_text(encoding="utf-8"), names)


def subset_sha256(text: str) -> str:
    """Digest of a subset exactly as termux_prepare.py writes it (UTF-8 bytes)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git_subset_sha256(repo: str, rev: str) -> dict[str, str]:
    """Subset digests of both locks at one commit, read with ``git show``."""
    if not _REV.fullmatch(rev):
        raise LockSubsetError("invalid_rev")
    found = {}
    for key, relative, names in LOCK_SOURCES:
        result = subprocess.run(["git", "-C", repo, "show", f"{rev}:{relative}"],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise LockSubsetError("lock_unreadable_at_rev")
        if len(result.stdout) > MAX_LOCK_BYTES:
            raise LockSubsetError("invalid_lock_file")
        found[key] = subset_sha256(subset_text(result.stdout.decode("utf-8"), names))
    return found


def main(argv: list[str] | None = None) -> int:
    """``--compare-git REPO REV_A REV_B``: print equal (0) / different (1) / undecided:<why> (2)."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4 or args[0] != "--compare-git":
        print("undecided:usage")
        return 2
    repo, first, second = args[1:]
    try:
        a, b = git_subset_sha256(repo, first), git_subset_sha256(repo, second)
    except LockSubsetError as exc:
        print(f"undecided:{exc}")
        return 2
    except (OSError, ValueError, subprocess.SubprocessError):
        print("undecided:io_error")
        return 2
    print("equal" if a == b else "different")
    print(json.dumps({first: a, second: b}, sort_keys=True), file=sys.stderr)
    return 0 if a == b else 1


if __name__ == "__main__":
    raise SystemExit(main())
