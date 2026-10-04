"""Optional node-local STE prose rules shared by Codex and Danso.

The Claude hook reads the same installed flag and rule. No conversation,
credentials, or audience state is read here. Missing/invalid files disable
this optional style; they must not prevent an owner request from running.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Mapping


def _read_regular(path: Path, limit: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o022 or info.st_size > limit):
            raise ValueError("invalid report-style file")
        data = os.read(descriptor, limit + 1)
        return data.decode("utf-8") if len(data) <= limit else ""
    finally:
        os.close(descriptor)


def read_report_style(claude_dir: Path, *, environ: Mapping[str, str]) -> str:
    """Re-read the flag on each invocation, independently of worker HOME."""
    if environ.get("CLAUDE_DISTILL_INFLIGHT"):
        return ""
    try:
        flag = claude_dir / "state/report-style-canary.flag"
        # An empty flag is valid. Validate it before distinguishing an empty
        # note from a missing/unsafe flag.
        info = flag.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o022 or info.st_size > 4096):
            return ""
        note_text = _read_regular(flag, 4096)
        hook_dir = Path(environ.get("CCC_HOOK_DIR") or claude_dir / "hooks")
        rule = _read_regular(hook_dir / "lib/report-style-ste.txt", 16384).rstrip()
        if not rule.strip():
            return ""
        note = next((line[:160] for line in note_text.splitlines() if line.strip()), "")
        return rule + (f"\n(카나리 메모: {note})" if note else "")
    except (OSError, UnicodeError, ValueError):
        return ""


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) == 3 and sys.argv[2] in {"SessionStart", "PostCompact"}:
        context = read_report_style(Path(sys.argv[1]), environ=os.environ)
        if context:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": sys.argv[2], "additionalContext": context,
            }}, ensure_ascii=False))
