#!/usr/bin/env python3
"""Changelog fragments: one file per change, assembled into the changelog at release (#2022).

Every PR used to prepend its entry to the top of ``CHANGELOG.md`` or
``bridge/CHANGELOG.md``. Two open PRs therefore always conflicted on the same
line, and on a merge-queue repo each conflict meant: merge main, a new head,
a full CI rerun, a fresh exact-head approval and a re-enqueue — for code that
did not overlap at all. A PR now adds its own file instead:

    changelog.d/<issue>-<slug>.md          -> CHANGELOG.md (under ``## [Unreleased]``)
    bridge/changelog.d/<issue>-<slug>.md   -> bridge/CHANGELOG.md (top of the list)

A fragment is ordinary changelog markdown: one or more ``- `` bullets, no
headings. ``README.md`` in a fragment directory is documentation, never a
fragment. At release time ``apply`` inserts every fragment (highest issue
number first, the order prepending produced) and deletes the files.

Commands (stdlib only, run from anywhere inside the checkout):

    changelog_fragments.py check            # validate fragments (CI)
    changelog_fragments.py check --none-pending   # also fail if CHANGELOG.md ones are pending (release)
    changelog_fragments.py preview          # print what apply would insert
    changelog_fragments.py apply            # insert and delete fragments
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
README = "README.md"
# The release notes are extracted from this target's changelog only.
RELEASE_NOTES_TARGET = "harness"
_NAME = re.compile(r"^(\d+)-[a-z0-9][a-z0-9._-]*\.md$")


@dataclass(frozen=True)
class Target:
    """A changelog and the fragment directory that feeds it."""

    label: str
    changelog: Path
    fragments: Path
    # Line after which fragments are inserted; the first match wins.
    anchor: re.Pattern[str]


def targets(root: Path = REPO_ROOT) -> list[Target]:
    return [
        Target("harness", root / "CHANGELOG.md", root / "changelog.d", re.compile(r"^## \[Unreleased\]\s*$")),
        Target("bridge", root / "bridge" / "CHANGELOG.md", root / "bridge" / "changelog.d", re.compile(r"^# Changelog\s*$")),
    ]


class FragmentError(ValueError):
    pass


def fragment_files(target: Target) -> list[Path]:
    """Fragments of one target, newest (highest leading number) first."""
    if not target.fragments.is_dir():
        return []
    files = [p for p in target.fragments.iterdir() if p.is_file() and p.name != README and p.suffix == ".md"]

    def key(path: Path) -> tuple[int, str]:
        match = _NAME.match(path.name)
        return (-(int(match.group(1)) if match else 0), path.name)

    return sorted(files, key=key)


def validate(path: Path) -> str:
    """The fragment's text, normalised to end with one newline; raises FragmentError."""
    if not _NAME.match(path.name):
        raise FragmentError(f"{path}: name must be <issue-number>-<slug>.md (lowercase slug)")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise FragmentError(f"{path}: not UTF-8") from None
    body = text.strip("\n")
    if not body.strip():
        raise FragmentError(f"{path}: empty")
    if not body.startswith("- "):
        raise FragmentError(f"{path}: must start with a '- ' bullet")
    fenced = False
    for number, line in enumerate(body.splitlines(), 1):
        if line.startswith(("<<<<<<<", ">>>>>>>", "=======")):
            raise FragmentError(f"{path}:{number}: merge conflict marker")
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced and re.match(r"#{1,6}(\s|$)", line):
            raise FragmentError(f"{path}:{number}: headings are not allowed in a fragment")
    return body + "\n"


def insertion(target: Target) -> tuple[str, list[Path]]:
    """The block to insert (a tight list, newest first) and the files it came from."""
    files = fragment_files(target)
    blocks = [validate(path) for path in files]
    # Tight list, like both changelogs: no blank line between entries.
    return "".join(blocks), files


def assemble(text: str, block: str, anchor: re.Pattern[str], *, label: str) -> str:
    """Insert ``block`` after the anchor line and its following blank line."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if anchor.match(line.rstrip("\n")):
            cut = index + 1
            while cut < len(lines) and not lines[cut].strip():
                cut += 1
            head = "".join(lines[:index + 1]) + "\n"
            rest = "".join(lines[cut:])
            # The new entries join the existing list directly (tight list); a
            # following "## " section keeps its blank-line separation.
            if rest and rest.startswith("#"):
                return head + block + "\n" + rest
            return head + block + rest
    raise FragmentError(f"{label}: anchor {anchor.pattern!r} not found in the changelog")


def cmd_check(none_pending: bool, root: Path) -> int:
    errors: list[str] = []
    pending = 0
    release_pending = 0
    for target in targets(root):
        for path in fragment_files(target):
            pending += 1
            try:
                validate(path)
            except FragmentError as error:
                errors.append(str(error))
        # Anything other than *.md files (and the README) is a mistake too:
        # other suffixes and subdirectories would be silently never applied.
        if target.fragments.is_dir():
            for path in target.fragments.iterdir():
                if path.is_dir():
                    errors.append(f"{path}: fragments live directly in {target.fragments.name}/, not in subdirectories")
                elif path.name != README and path.suffix != ".md":
                    errors.append(f"{path}: fragments must be .md files")
        if target.label == RELEASE_NOTES_TARGET:
            release_pending = len(fragment_files(target))
    for error in errors:
        print(f"changelog fragment: {error}", file=sys.stderr)
    if errors:
        return 1
    if none_pending and release_pending:
        # Only CHANGELOG.md feeds the release notes; bridge fragments can wait
        # for the next apply without making the notes incomplete.
        print(
            f"changelog fragment: {release_pending} CHANGELOG.md fragment(s) pending — run "
            "`python3 scripts/changelog_fragments.py apply` in the release PR and tag its merge commit",
            file=sys.stderr,
        )
        return 1
    print(f"changelog fragments ok ({pending} pending)")
    return 0


def cmd_preview(root: Path) -> int:
    for target in targets(root):
        block, files = insertion(target)
        if files:
            print(f"== {target.changelog.relative_to(root)} ({len(files)} fragment(s))")
            print(block)
    return 0


def cmd_apply(root: Path) -> int:
    planned = []
    for target in targets(root):
        block, files = insertion(target)
        if not files:
            continue
        text = target.changelog.read_text(encoding="utf-8")
        planned.append((target, assemble(text, block, target.anchor, label=target.label), files))
    # Validate and assemble everything before writing anything.
    for target, text, files in planned:
        target.changelog.write_text(text, encoding="utf-8")
        for path in files:
            path.unlink()
        print(f"{target.changelog.relative_to(root)}: {len(files)} fragment(s) applied")
    if not planned:
        print("no changelog fragments pending")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--none-pending", action="store_true")
    sub.add_parser("preview")
    sub.add_parser("apply")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return cmd_check(args.none_pending, args.root)
        if args.command == "preview":
            return cmd_preview(args.root)
        return cmd_apply(args.root)
    except FragmentError as error:
        print(f"changelog fragment: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
