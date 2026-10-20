#!/usr/bin/env python3
"""Aggregate context-cost stats from recent Claude Code session transcripts (#2262).

Scans ``<root>/**/*.jsonl`` (default ``${CLAUDE_CONFIG_DIR:-~/.claude}/projects``)
for files modified in the last ``--days`` days and reports, as aggregates only:

1. compactions — ``type=system`` / ``subtype=compact_boundary`` entries,
2. tool results — count, total chars, and the share of chars held by results
   larger than ``--threshold`` chars (default 20,000),
3. top tools and top files by result size (files as BASENAME only),
4. same-file re-Reads — a ``Read`` of a basename (same offset/limit) already
   read earlier in the same transcript file, in any directory, so re-reading
   ``page_06.png`` once per rendered document version counts while paging
   through one file does not.

Privacy contract: no message bodies, tool inputs, full paths, project
directory names or session ids are ever printed — only counts, sizes, tool
names and truncated file basenames. Malformed lines and unreadable files are
skipped and counted. Read-only; stdlib only.

Result size: a string ``tool_result`` counts its length; a block list counts
text lengths plus the base64 ``data`` length of image blocks (an image's
context cost is resolution-based, so chars are a proxy, not a token count).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

DEFAULT_DAYS = 14
DEFAULT_THRESHOLD = 20_000
DEFAULT_TOP = 5
BASENAME_MAX = 48
ONE_LINE_BASENAME_MAX = 32
FILE_INPUT_KEYS = ("file_path", "notebook_path")


def default_root() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.environ.get("HOME") or os.path.expanduser("~"), ".claude"
    )
    return Path(base) / "projects"


def safe_basename(path: object) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    name = os.path.basename(path.rstrip("/\\")) or "?"
    name = "".join(ch if ch.isprintable() else "?" for ch in name)
    if len(name) > BASENAME_MAX:
        name = name[: BASENAME_MAX - 1] + "~"
    return name


def safe_tool(name: object) -> str:
    if not isinstance(name, str) or not name:
        return "?"
    return "".join(ch if ch.isprintable() else "?" for ch in name)[:64]


def result_chars(content: object) -> tuple[int, bool]:
    """(chars, has_image) of a tool_result ``content`` value."""
    if isinstance(content, str):
        return len(content), False
    if not isinstance(content, list):
        return 0, False
    total, image = 0, False
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            total += len(block["text"])
        elif kind == "image":
            image = True
            source = block.get("source")
            if isinstance(source, dict) and isinstance(source.get("data"), str):
                total += len(source["data"])
    return total, image


def parse_ts(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Stats:
    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.sessions = 0
        self.subagent_files = 0
        self.unreadable_files = 0
        self.malformed_lines = 0
        self.compactions = 0
        self.compaction_triggers: Counter[str] = Counter()
        self.results = 0
        self.chars = 0
        self.big_results = 0
        self.big_chars = 0
        self.image_results = 0
        self.tool_count: Counter[str] = Counter()
        self.tool_chars: Counter[str] = Counter()
        self.tool_big: Counter[str] = Counter()
        self.file_chars: Counter[str] = Counter()
        self.file_reads: Counter[str] = Counter()
        self.rereads = 0
        self.file_rereads: Counter[str] = Counter()

    # -- per transcript file ---------------------------------------------
    def scan_file(self, path: Path, cutoff: float) -> None:
        uses: dict[str, tuple[str, str | None]] = {}
        seen_reads: set[str] = set()
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            self.unreadable_files += 1
            return
        with handle:
            for line in handle:
                entry = self._parse(line)
                if entry is None:
                    continue
                ts = parse_ts(entry.get("timestamp"))
                if ts is not None and ts < cutoff:
                    continue
                self._entry(entry, uses, seen_reads)

    def _parse(self, line: str) -> dict | None:
        line = line.strip()
        if not line:
            return None
        try:
            entry = json.loads(line)
        except ValueError:
            self.malformed_lines += 1
            return None
        if not isinstance(entry, dict):
            self.malformed_lines += 1
            return None
        return entry

    def _entry(self, entry: dict, uses: dict, seen_reads: set[str]) -> None:
        if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
            self.compactions += 1
            meta = entry.get("compactMetadata")
            trigger = meta.get("trigger") if isinstance(meta, dict) else None
            self.compaction_triggers[trigger if trigger in ("auto", "manual") else "other"] += 1
            return
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                self._tool_use(block, uses, seen_reads)
            elif block.get("type") == "tool_result":
                self._tool_result(block, uses)

    def _tool_use(self, block: dict, uses: dict, seen_reads: set[str]) -> None:
        name = safe_tool(block.get("name"))
        args = block.get("input") if isinstance(block.get("input"), dict) else {}
        raw_path = next((args[k] for k in FILE_INPUT_KEYS if isinstance(args.get(k), str)), None)
        base = safe_basename(raw_path)
        use_id = block.get("id")
        if isinstance(use_id, str):
            uses[use_id] = (name, base)
        if name == "Read" and base:
            self.file_reads[base] += 1
            # Same basename + same range, in any directory: a render re-read per
            # document version (/tmp/render-v1/page_06.png, …-v2/page_06.png)
            # counts, paging through one file with new offsets does not.
            key = f"{base}\0{args.get('offset')}\0{args.get('limit')}"
            if key in seen_reads:
                self.rereads += 1
                self.file_rereads[base] += 1
            seen_reads.add(key)

    def _tool_result(self, block: dict, uses: dict) -> None:
        use_id = block.get("tool_use_id")
        name, base = uses.get(use_id, ("?", None)) if isinstance(use_id, str) else ("?", None)
        size, image = result_chars(block.get("content"))
        self.results += 1
        self.chars += size
        self.tool_count[name] += 1
        self.tool_chars[name] += size
        if image:
            self.image_results += 1
        if size > self.threshold:
            self.big_results += 1
            self.big_chars += size
            self.tool_big[name] += 1
        if base:
            self.file_chars[base] += size

    # -- output ------------------------------------------------------------
    def report(self, days: int, top: int) -> dict:
        share = round(self.big_chars / self.chars, 4) if self.chars else 0.0
        return {
            "days": days,
            "threshold_chars": self.threshold,
            "sessions": self.sessions,
            "subagent_files": self.subagent_files,
            "unreadable_files": self.unreadable_files,
            "malformed_lines": self.malformed_lines,
            "compactions": self.compactions,
            "compaction_triggers": dict(sorted(self.compaction_triggers.items())),
            "tool_results": self.results,
            "tool_result_chars": self.chars,
            "image_results": self.image_results,
            "over_threshold_results": self.big_results,
            "over_threshold_chars": self.big_chars,
            "over_threshold_char_share": share,
            "top_tools": [
                {
                    "tool": name,
                    "results": self.tool_count[name],
                    "chars": chars,
                    "over_threshold": self.tool_big[name],
                }
                for name, chars in self.tool_chars.most_common(top)
            ],
            "top_files": [
                {"file": name, "chars": chars, "reads": self.file_reads[name]}
                for name, chars in self.file_chars.most_common(top)
            ],
            "rereads": self.rereads,
            "top_rereads": [
                {"file": name, "rereads": count}
                for name, count in self.file_rereads.most_common(top)
            ],
        }


def human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def one_line(rep: dict) -> str:
    thr = rep["threshold_chars"]
    thr_label = f">{thr // 1000}k" if thr % 1000 == 0 else f">{thr}"
    parts = [
        f"context-stats {rep['days']}d: sessions={rep['sessions']}",
        f"compactions={rep['compactions']}",
        f"tool_results={rep['tool_results']} ({human(rep['tool_result_chars'])} chars)",
        f"{thr_label}={rep['over_threshold_results']} "
        f"({rep['over_threshold_char_share'] * 100:.0f}% of chars)",
    ]
    if rep["top_tools"]:
        t = rep["top_tools"][0]
        parts.append(f"top tool={t['tool']} {human(t['chars'])}")
    reread = f"rereads={rep['rereads']}"
    if rep["top_rereads"]:
        r = rep["top_rereads"][0]
        name = r["file"]
        if len(name) > ONE_LINE_BASENAME_MAX:
            name = name[: ONE_LINE_BASENAME_MAX - 1] + "~"
        reread += f" (top {name} x{r['rereads']})"
    parts.append(reread)
    if rep["malformed_lines"] or rep["unreadable_files"]:
        parts.append(f"skipped={rep['malformed_lines']} lines/{rep['unreadable_files']} files")
    return " · ".join(parts)


def text_report(rep: dict) -> str:
    lines = [one_line(rep), f"compaction triggers: {rep['compaction_triggers'] or '{}'}"]
    lines.append(f"image results: {rep['image_results']}; subagent files: {rep['subagent_files']}")
    lines.append("top tools (results / chars / over threshold):")
    lines += [
        f"  {t['tool']}: {t['results']} / {human(t['chars'])} / {t['over_threshold']}"
        for t in rep["top_tools"]
    ] or ["  (none)"]
    lines.append("top files by result chars (basename / chars / reads):")
    lines += [
        f"  {f['file']}: {human(f['chars'])} / {f['reads']}" for f in rep["top_files"]
    ] or ["  (none)"]
    lines.append("top same-file re-Reads (basename / rereads):")
    lines += [f"  {r['file']}: {r['rereads']}" for r in rep["top_rereads"]] or ["  (none)"]
    return "\n".join(lines)


def collect(root: Path, days: int, threshold: int) -> Stats:
    stats = Stats(threshold)
    cutoff = time.time() - days * 86400
    if not root.is_dir():
        return stats
    for path in sorted(root.rglob("*.jsonl")):
        try:
            if not path.is_file() or path.stat().st_mtime < cutoff:
                continue
        except OSError:
            stats.unreadable_files += 1
            continue
        if "subagents" in path.relative_to(root).parts[:-1]:
            stats.subagent_files += 1
        else:
            stats.sessions += 1
        stats.scan_file(path, cutoff)
    return stats


def positive_int(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=positive_int, default=DEFAULT_DAYS)
    ap.add_argument("--root", type=Path, default=None,
                    help="transcript root (default: ${CLAUDE_CONFIG_DIR:-~/.claude}/projects)")
    ap.add_argument("--threshold", type=positive_int, default=DEFAULT_THRESHOLD)
    ap.add_argument("--top", type=positive_int, default=DEFAULT_TOP)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="machine-readable aggregates")
    mode.add_argument("--one-line", action="store_true", help="single summary line")
    args = ap.parse_args(argv)

    rep = collect(args.root or default_root(), args.days, args.threshold).report(args.days, args.top)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, sort_keys=True))
    elif args.one_line:
        print(one_line(rep))
    else:
        print(text_report(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
