#!/usr/bin/env python3
"""Shared path and managed-artifact safety checks for ccc-node installers."""

from __future__ import annotations

import os
from pathlib import Path, PurePath
import stat
import sys
from typing import NoReturn


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def normalized(raw: str) -> str:
    return os.path.normpath(os.path.join(os.path.sep, os.path.relpath(raw, os.path.sep)))


def check_components(path: str, message_prefix: str) -> None:
    current = Path(os.path.sep)
    for part in PurePath(path).parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            fail(f"{message_prefix}: {current}")


def overlaps(first: str, second: str) -> bool:
    return first == second or first.startswith(second + os.sep) or second.startswith(first + os.sep)


def setup_roots(args: list[str]) -> None:
    if len(args) != 2:
        fail("ERROR: setup root validator requires Claude and Hermes paths")
    labels = ("CCC_CLAUDE_DIR", "CCC_HERMES_DIR")
    values: list[str] = []
    for label, raw in zip(labels, args):
        if not raw or not os.path.isabs(raw):
            fail(f"ERROR: {label} must be a non-empty absolute path")
        path = normalized(raw)
        if path == os.path.sep:
            fail(f"ERROR: refusing filesystem-root install path for {label}")
        check_components(path, "ERROR: install path contains symlink component")
        values.append(path)
    if overlaps(values[0], values[1]):
        fail("ERROR: Claude and Hermes install roots must not overlap")


def setup_guard_profile(args: list[str]) -> None:
    if len(args) != 1:
        fail("ERROR: guard profile validator requires one path")
    raw = args[0]
    if not raw or not os.path.isabs(raw):
        fail("ERROR: guard profile path must be a non-empty absolute path")
    path = normalized(raw)
    if path == os.path.sep:
        fail("ERROR: refusing filesystem-root guard profile path")
    check_components(path, "ERROR: guard profile path contains symlink component")


def self_update_roots(args: list[str]) -> None:
    if len(args) != 3:
        fail("self-update: runtime validator requires Claude, Hermes, and state paths")
    values: list[str] = []
    for raw in args:
        if not raw or not os.path.isabs(raw):
            fail(f"self-update: path must be absolute: {raw!r}")
        path = normalized(raw)
        if path == os.path.sep:
            fail("self-update: refusing filesystem-root path")
        check_components(path, "self-update: path contains symlink component")
        values.append(path)
    claude, hermes, state = values
    if overlaps(claude, hermes):
        fail("self-update: Claude and Hermes roots must not overlap")
    if not (state == claude or state.startswith(claude + os.sep)):
        fail("self-update: state directory must be inside the Claude root")


def self_update_repo(args: list[str]) -> None:
    if len(args) != 3:
        fail("self-update: repository validator requires repository, Claude, and Hermes paths")
    raw, claude_raw, hermes_raw = args
    if not raw or not os.path.isabs(raw):
        fail("self-update: repository path must be absolute")
    repo = normalized(raw)
    if repo == os.path.sep:
        fail("self-update: refusing filesystem-root repository")
    for other_raw in (claude_raw, hermes_raw):
        other = normalized(other_raw)
        if overlaps(repo, other):
            fail("self-update: repository and install roots must not overlap")
    check_components(repo, "self-update: repository path contains symlink component")


def managed_artifacts(args: list[str]) -> None:
    if len(args) < 4:
        fail("managed-artifacts: prefix, Claude root, Hermes root, and paths are required")
    prefix, claude_raw, hermes_raw, *items = args
    claude = Path(claude_raw)
    hermes = Path(hermes_raw)
    for item in items:
        root = claude / item
        candidates = [root]
        if root.is_dir() and not root.is_symlink():
            candidates.extend(root.rglob("*"))
        for candidate in candidates:
            if candidate.is_symlink():
                fail(f"{prefix} refusing managed artifact symlink: {candidate}")
            try:
                mode = candidate.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISREG(mode) and candidate.stat().st_nlink != 1:
                fail(f"{prefix} refusing managed artifact hardlink: {candidate}")
    honcho = hermes / "honcho.json"
    if honcho.is_symlink():
        fail(f"{prefix} refusing managed artifact symlink: {honcho}")
    if honcho.exists() and honcho.is_file() and honcho.stat().st_nlink != 1:
        fail(f"{prefix} refusing managed artifact hardlink: {honcho}")


def main() -> None:
    if len(sys.argv) < 2:
        fail("usage: harness_paths.py <mode> [args...]")
    mode, args = sys.argv[1], sys.argv[2:]
    handlers = {
        "setup-roots": setup_roots,
        "setup-guard-profile": setup_guard_profile,
        "self-update-roots": self_update_roots,
        "self-update-repo": self_update_repo,
        "managed-artifacts": managed_artifacts,
    }
    handler = handlers.get(mode)
    if handler is None:
        fail(f"unknown harness path mode: {mode}")
    handler(args)


if __name__ == "__main__":
    main()
