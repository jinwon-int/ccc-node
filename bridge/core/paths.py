"""Pure path-scope classification for the Telegram bridge permission gate.

These helpers decide which file paths a tool call would touch and whether they
fall outside ``PROJECT_ROOT`` (and therefore need explicit user approval). They
were extracted from ``core/bot.py`` so this security-relevant logic can be unit
tested directly with an arbitrary project root, instead of only through the live
bot. The functions are pure: the project root is passed in rather than read from
module globals, and they perform no I/O. ``TelegramBot`` keeps thin delegating
methods that supply the current ``PROJECT_ROOT``, so behavior is unchanged.
"""

from __future__ import annotations

import shlex
from pathlib import Path as FilePath
from typing import Any, Iterable, Iterator, List, Sequence, Tuple

# Tools whose inputs can reference filesystem paths we must scope-check.
PATH_GUARDED_TOOLS = frozenset(
    {"Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "Bash"}
)

# Dict-key substrings that mark a string value as a path to scope-check.
PATH_KEYWORDS: Tuple[str, ...] = ("path", "file", "cwd", "dir", "directory", "root")


def is_within_project_root(path: FilePath, project_root: FilePath) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(project_root)
    except Exception:
        return False


def resolve_candidate_path(raw_path: str, project_root: FilePath) -> FilePath:
    candidate = FilePath(raw_path.strip().strip("\"'")).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve(strict=False)


def iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_strings(item)


def extract_paths_from_command(command: str) -> List[str]:
    try:
        tokens = shlex.split(command)
    except Exception:
        tokens = command.split()

    candidates: List[str] = []
    for token in tokens:
        token = token.strip()
        if not token or token.startswith("-") or "://" in token:
            continue
        if token.startswith(("~", "/", "./", "../")) or "/" in token:
            candidates.append(token)
    return candidates


def extract_path_candidates(
    tool_name: str,
    tool_input: Any,
    path_keywords: Iterable[str] = PATH_KEYWORDS,
) -> List[str]:
    candidates: List[str] = []
    seen = set()

    def add_candidate(raw: str):
        raw = raw.strip()
        if not raw or raw in seen:
            return
        seen.add(raw)
        candidates.append(raw)

    def walk(value: Any, parent_key: str = ""):
        if isinstance(value, dict):
            for key, item in value.items():
                key_lower = key.lower()
                if isinstance(item, str) and any(
                    word in key_lower for word in path_keywords
                ):
                    add_candidate(item)
                else:
                    walk(item, key_lower)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item, parent_key)
            return
        if isinstance(value, str) and parent_key == "command":
            for token in extract_paths_from_command(value):
                add_candidate(token)

    walk(tool_input)
    if tool_name == "Bash":
        for text in iter_strings(tool_input):
            for token in extract_paths_from_command(text):
                add_candidate(token)
    return candidates


def extract_outside_paths(
    tool_name: str,
    tool_input: Any,
    *,
    project_root: FilePath,
    path_keywords: Iterable[str] = PATH_KEYWORDS,
    guarded_tools: Iterable[str] = PATH_GUARDED_TOOLS,
) -> List[str]:
    if tool_name not in guarded_tools:
        return []
    outside: List[str] = []
    seen = set()
    for raw_path in extract_path_candidates(tool_name, tool_input, path_keywords):
        try:
            resolved = resolve_candidate_path(raw_path, project_root)
        except Exception:
            continue
        if not is_within_project_root(resolved, project_root):
            path_str = str(resolved)
            if path_str not in seen:
                seen.add(path_str)
                outside.append(path_str)
    return outside


def split_paths_by_scope(
    paths: Sequence[FilePath], project_root: FilePath
) -> Tuple[List[FilePath], List[FilePath]]:
    in_root: List[FilePath] = []
    outside: List[FilePath] = []
    for path in paths:
        if is_within_project_root(path, project_root):
            in_root.append(path)
        else:
            outside.append(path)
    return in_root, outside
