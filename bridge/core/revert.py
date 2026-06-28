"""Conversation-JSONL truncation for the /revert flow.

Extracted from the ``TelegramBot`` god object. This is the data-loss-sensitive
core of revert: it rewrites a session's SDK conversation ``.jsonl`` so the
conversation is rolled back to the state *before* a chosen message. Keeping it
here — decoupled from Telegram/session orchestration — lets the truncation and
its atomic-write/error handling be unit tested directly against real files.

The surrounding orchestration (resolving the session file under the
conversations dir, cancelling streams/tasks, resetting session state) stays in
``bot.py`` and calls :func:`truncate_jsonl_to` with an already-resolved path.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path as FilePath
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)


def lines_to_keep(lines: Sequence[str], msg_index: int) -> List[str]:
    """The lines strictly before ``msg_index``.

    Reverting to message ``msg_index`` keeps everything up to but NOT including
    it, so the conversation returns to the state before that message. A
    non-positive index keeps nothing; an index past the end keeps everything.
    """
    if msg_index <= 0:
        return []
    return list(lines[:msg_index])


def truncate_jsonl_to(filepath: FilePath, msg_index: int) -> bool:
    """Atomically truncate *filepath* to the lines before ``msg_index``.

    Returns True on success, False if the file is missing or any error occurs
    (the original file is left untouched on failure). Writes via a temp file in
    the same directory and ``os.replace`` so readers never see a partial file.
    """
    if not filepath.exists():
        return False

    tmp_path: Optional[FilePath] = None
    try:
        # Read only up to the target message; we never need lines at/after it.
        keep: List[str] = []
        with open(filepath, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if idx >= msg_index:
                    break
                keep.append(line)

        tmp_path = filepath.with_name(
            f".{filepath.name}.tmp-{os.getpid()}-{time.time_ns()}"
        )
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp_path, filepath)
        return True

    except Exception as e:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        logger.error(f"Conversation revert failed: {e}", exc_info=True)
        return False
