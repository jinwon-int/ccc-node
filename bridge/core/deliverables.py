"""Which files an agent answer names as deliverables (shared by Telegram and Matrix).

The Telegram bridge has always sent a real file an answer mentions (documents,
spreadsheets, archives, media — not source code) back to the chat. The Matrix
frontend (#2001) uses the same rule, so the extension list and path pattern
live here; ``TelegramBot`` keeps its class attributes pointing at them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

# Deliberately excludes source-code extensions, so an agent narrating its work
# ("I edited src/app.py") does not push every touched file to the chat. A
# matched path must still pass the is_file()/size/scope gate before anything
# is sent, so a false-positive token that is not a real file is harmless.
SENDABLE_FILE_EXTENSIONS = (
    # documents
    "pdf", "txt", "md", "markdown", "rtf", "doc", "docx", "odt", "tex", "epub",
    # Korean word processor (Hangul) documents
    "hwp", "hwpx",
    # data / markup
    "csv", "tsv", "json", "jsonl", "ndjson", "xml", "yaml", "yml", "ics", "log",
    # spreadsheets / presentations
    "xls", "xlsx", "ods", "ppt", "pptx", "odp",
    # archives
    "zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar",
    # images
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif", "svg", "heic",
    # audio
    "mp3", "wav", "ogg", "oga", "m4a", "flac", "aac", "opus", "amr",
    # video
    "mp4", "mov", "webm", "mkv", "avi", "m4v",
)
# Extensions that are deliverables ONLY under an ``artifacts/`` directory
# (#2109 C). An HTML file is usually web source an ordinary coding turn edits
# ("I changed public/index.html"), so it stays off the general list; a
# generated single-file report the agent writes under
# ``…/artifacts/…`` (e.g. ``~/.claude/state/artifacts/fleet-matrix.html``) is
# the owner's deliverable and is sent like any document.
ARTIFACT_ONLY_EXTENSIONS = ("html", "htm")
ARTIFACT_DIR_NAME = "artifacts"
# Match both absolute (/foo/bar.pdf) and relative (foo/bar.pdf) file paths.
# A directory separator is required (reduces prose false-positives), and the
# trailing (?![A-Za-z0-9]) makes the extension alternation order-independent
# and stops partial matches (e.g. ".json" is not clipped to ".js"). The second
# alternative admits ARTIFACT_ONLY_EXTENSIONS only when an ``artifacts``
# directory component precedes the file name. Inner groups are non-capturing
# so ``findall`` keeps returning the whole path.
FILE_PATH_RE = re.compile(
    r"((?:/?(?:[\w.@-]+/)+[\w.@-]+\.(?:"
    + "|".join(SENDABLE_FILE_EXTENSIONS)
    + r"))|(?:/?(?:[\w.@-]+/)*"
    + ARTIFACT_DIR_NAME
    + r"/(?:[\w.@-]+/)*[\w.@-]+\.(?:"
    + "|".join(ARTIFACT_ONLY_EXTENSIONS)
    + r")))(?![A-Za-z0-9])",
    re.IGNORECASE,
)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def resolve_deliverable_paths(content: str, project_root: Path, *, max_bytes: int) -> List[Path]:
    """Real, readable files ``content`` names, resolved and de-duplicated, in order.

    Relative paths resolve against ``project_root``. A path the process cannot
    stat (another user's private directory, a traversal-denied mountpoint) is
    not a deliverable and is skipped rather than raised (#1332).
    """
    paths: List[Path] = []
    seen = set()
    for match in FILE_PATH_RE.findall(content or ""):
        path = Path(match.strip())
        if not path.is_absolute():
            path = Path(project_root) / path
        try:
            path = path.resolve()
            deliverable = path.is_file() and path.stat().st_size < max_bytes
        except OSError:
            continue
        if path not in seen and deliverable:
            seen.add(path)
            paths.append(path)
    return paths


__all__ = [
    "ARTIFACT_DIR_NAME",
    "ARTIFACT_ONLY_EXTENSIONS",
    "FILE_PATH_RE",
    "IMAGE_EXTS",
    "SENDABLE_FILE_EXTENSIONS",
    "resolve_deliverable_paths",
]
