#!/usr/bin/env python3
"""Retired memory-layer guidance: detection (doctor) and refresh (setup) (#2238).

``setup.sh`` seeds the node-owned ``~/.claude/CLAUDE.md`` and MEMORY.md files
once and never overwrites them, so nodes seeded before the Honcho retirement
(2026-09-01, #1436) kept telling every session to recall from Honcho. This
module is the single home of that knowledge:

* detection — ``stale_memory_guidance_hits`` / ``memory_stack_span``, used by
  ``ccc_doctor.py``'s read-only ``stale-memory-guidance`` row;
* refresh — ``setup.sh --refresh-guidance`` runs this file as a script. It backs
  up each target and replaces only (a) the marker-delimited Memory Stack block,
  (b) on a legacy file without markers, the ``## Memory Stack`` .. ``## Fresh
  Approval Required`` span when it is stale *and* equals a known old template
  version, and (c) single lines that equal a known old template line exactly.
  Anything else is refused and left untouched: a node-customized span is the
  operator's to fix by hand.

The report names files, line numbers and pattern labels only — never file text.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# --- detection (shared with ccc_doctor.py) ----------------------------------

# Each pattern is one shape the old templates used; ``label`` names it in reports.
_RETIRED_LAYER = r"(?:Honcho|Supermemory)"
_STALE_MEMORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "built-in MEMORY/USER + Honcho + Family Wiki + session_search"
    ("stack-list", re.compile(rf"\+\s*\**{_RETIRED_LAYER}\b|\b{_RETIRED_LAYER}\**\s*\+")),
    # "**cached Honcho working memory**" (SessionStart snapshot description)
    ("cached-layer", re.compile(rf"\bcached\s+{_RETIRED_LAYER}\b", re.IGNORECASE)),
    # "**Honcho — working/relational memory:**" section heading
    ("layer-heading", re.compile(rf"\b{_RETIRED_LAYER}\**\s*[—–-]+\s*working", re.IGNORECASE)),
    # "POST {baseUrl}/v3/workspaces/<ws>/peers/<peer>/chat" Honcho recall API
    ("recall-endpoint", re.compile(r"/v3/workspaces/\S*/peers/")),
    # "`baseUrl` is in `~/.hermes/honcho.json`"
    ("honcho-config", re.compile(r"honcho\.json", re.IGNORECASE)),
    # "Relational & evolving working memory → Honcho."
    ("routing", re.compile(rf"(?:→|->|=>)\s*\**{_RETIRED_LAYER}\b")),
    # "Current memory stack: ... Honcho ..."
    (
        "current-stack",
        re.compile(rf"\bcurrent\s+(?:memory\s+)?stack\b.*\b{_RETIRED_LAYER}\b", re.IGNORECASE),
    ),
    # "Read Family Wiki, Honcho, local MEMORY/USER, ..." (reads table row)
    ("read-source", re.compile(rf"\bRead\b[^|]*?\b{_RETIRED_LAYER}\b")),
)
# A sentence that says the layer is retired is the *fix*, not drift: the
# current template names honcho.json in "Honcho is retired ... do not query it".
_RETIRED_CONTEXT = re.compile(
    r"retire|퇴역|은퇴|legacy|deprecat|decommission|removed|폐기|제거|no longer"
    r"|do not (?:query|use)|don't (?:query|use)|사용\s*(?:금지|하지)",
    re.IGNORECASE,
)
# Sentence boundary inside one line; a period inside a path (honcho.json) is not.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?。])\s+")
STALE_MEMORY_READ_LIMIT = 1 << 20

MEMORY_STACK_START = "## Memory Stack"
MEMORY_STACK_END = "## Fresh Approval Required"
# Machine-readable bounds of the managed Memory Stack block in
# claude/CLAUDE.md.template. Everything between them (inclusive) is replaced
# wholesale by ``--refresh-guidance``; everything outside is node-owned.
MARKER_BEGIN = "<!-- ccc:memory-stack:begin -->"
MARKER_END = "<!-- ccc:memory-stack:end -->"
_MARKERS = frozenset({MARKER_BEGIN, MARKER_END})


def stale_memory_guidance_hits(text: str) -> list[tuple[int, list[str]]]:
    """``(line number, pattern labels)`` for lines that present a retired layer as current.

    Scanned per sentence so "Supermemory is retired. Current stack: ... + Honcho
    + ..." (the old MEMORY.md line) still flags its second sentence, while a
    sentence carrying a retirement word is exempt.
    """
    hits: list[tuple[int, list[str]]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        labels: list[str] = []
        for sentence in _SENTENCE_BREAK.split(line):
            if _RETIRED_CONTEXT.search(sentence):
                continue
            for label, pattern in _STALE_MEMORY_PATTERNS:
                if label not in labels and pattern.search(sentence):
                    labels.append(label)
        if labels:
            hits.append((lineno, labels))
    return hits


def _heading_bounds(lines: list[str]) -> tuple[int, int] | None:
    """``[start, end)`` of ``## Memory Stack`` .. (not including) ``## Fresh Approval Required``."""
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == MEMORY_STACK_START)
        end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip() == MEMORY_STACK_END)
    except StopIteration:
        return None
    return start, end


def memory_stack_span(text: str) -> str | None:
    """The ``## Memory Stack`` .. (not including) ``## Fresh Approval Required`` span.

    That span holds every memory-layer instruction in CLAUDE.md (Memory Stack +
    Live Memory Usage). Marker comment lines are dropped so a refreshed file
    (markers) and a hand-fixed one (no markers) both compare equal to the
    template. None when either heading is missing.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    bounds = _heading_bounds(lines)
    if bounds is None:
        return None
    start, end = bounds
    return "\n".join(line for line in lines[start:end] if line.strip() not in _MARKERS).strip()


# --- refresh ------------------------------------------------------------------

# The ``## Memory Stack`` .. ``## Fresh Approval Required`` span of every
# claude/CLAUDE.md.template version that still named Honcho as current: it was
# byte-identical from the initial commit be9398a4 through b0aec5ac and changed in
# 89314d75 (#2163). A legacy (marker-less) span is replaced only when it equals
# one of these after ``_normalize`` — a node-edited span never matches.
KNOWN_STALE_MEMORY_SPANS: tuple[str, ...] = (
    """## Memory Stack
- Current stack: **built-in MEMORY/USER + Honcho + Family Wiki + session_search**.
- **Supermemory is retired/legacy** — do not use unless explicitly approved for a specific incident lookup.
- Cleanup policy: latest instructions win; delete/compress boilerplate, duplicates, stale PR/process states, and mutable facts first.

## Live Memory Usage (every session — DO THIS)
The SessionStart hook (`~/.claude/hooks/load-memory.sh`) auto-injects a snapshot at startup: built-in MEMORY/USER + **cached Family Wiki prefetch** + **cached Honcho working memory** (caches refresh in the background each session). The snapshot is a **starting point, not a substitute** — actively query both sources during work, and the injected Wiki snippets are *candidates* that must be verified before any operational claim.

**Family Wiki — consult FIRST (before web / GitHub / session_search):**
- Search: `wiki-agent find "<query>"`. Verify: `wiki-agent load --lines A:B <path>`. Quick context: `wiki-agent prefetch "<query>"`.
- Durable update: `wiki-agent write-path` → edit in the PR worktree → `wiki-agent pr` (PR-first; never raw secrets, only locations/handling).

**Honcho — working/relational memory:**
- Recall (dialectic): `POST {baseUrl}/v3/workspaces/<workspace>/peers/<peerName>/chat` with `{"query":"…","target":"<target>","reasoning_level":"low"}`. `baseUrl` is in `~/.hermes/honcho.json` — never log credential/endpoint values.

**Routing:** durable facts / nodes / runbooks / decisions → Family Wiki. Relational & evolving working memory → Honcho.""",
)


@dataclass(frozen=True)
class KnownLine:
    """One old template line, rewritten only on an exact (trailing-space-insensitive) match.

    ``template_prefix`` locates the replacement in the *current* template: the
    single line that starts with it.
    """

    old: str
    template_prefix: str


# Outside the Memory Stack span, the old CLAUDE.md template also named Honcho
# in the Standing Orders reads row (ccc049c2 .. b0aec5ac).
CLAUDE_KNOWN_LINES: tuple[KnownLine, ...] = (
    KnownLine(
        "| Wiki / memory reads | Read Family Wiki, Honcho, local MEMORY/USER, and session history; "
        "verify Wiki snippets with source loads before operational claims. | User asks about infra, "
        "nodes, A2A, Hermes, Gateway, runbooks, durable knowledge, or prior context. | No approval "
        "needed for reads. Wiki writes still use PR-first `wiki-agent write-path/pr`; raw secrets are "
        "never stored. | If Wiki and live state disagree, live-check first, then update Wiki through "
        "PR with dated evidence. |",
        "| Wiki / memory reads |",
    ),
)
# The memory-stack line of hermes/memories/MEMORY.template.md (be9398a4 .. 15746bbd).
MEMORY_KNOWN_LINES: tuple[KnownLine, ...] = (
    KnownLine(
        "- Supermemory is retired/legacy. Current memory stack: built-in MEMORY/USER + Honcho + "
        "Family Wiki + session_search.",
        "- Supermemory is retired/legacy.",
    ),
)


class TemplateError(RuntimeError):
    """The repo template lacks the markers / lines the refresh relies on."""


def _normalize(lines: list[str]) -> str:
    """Comparison form: no markers, no trailing spaces, single blank runs, no outer blanks."""
    out: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if line.strip() in _MARKERS:
            continue
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip("\n")


def template_block(template_text: str) -> list[str]:
    """The marker-delimited block of the CLAUDE.md template, markers included."""
    lines = template_text.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == MARKER_BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == MARKER_END]
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        raise TemplateError(
            "claude/CLAUDE.md.template must hold exactly one memory-stack marker pair"
        )
    block = lines[begins[0] : ends[0] + 1]
    if stale_memory_guidance_hits("\n".join(block)):
        raise TemplateError("claude/CLAUDE.md.template memory-stack block is itself stale")
    return block


def _template_line(template_text: str, prefix: str) -> str:
    found = [line for line in template_text.splitlines() if line.startswith(prefix)]
    if len(found) != 1:
        raise TemplateError(f"template must hold exactly one line starting with {prefix!r}")
    if stale_memory_guidance_hits(found[0]):
        raise TemplateError(f"template line {prefix!r} is itself stale")
    return found[0]


@dataclass
class Outcome:
    """What the refresh did (or would do) to one file's text."""

    text: str
    refused: str = ""
    changes: list[str] = field(default_factory=list)
    remaining: list[tuple[int, list[str]]] = field(default_factory=list)


def _replace_known_lines(
    lines: list[str], known: tuple[KnownLine, ...], template_text: str, changes: list[str]
) -> None:
    for entry in known:
        replacement = None
        for i, line in enumerate(lines):
            if line.rstrip() == entry.old:
                replacement = replacement or _template_line(template_text, entry.template_prefix)
                lines[i] = replacement
                changes.append(f"L{i + 1} known stale line -> current template line")


def _refresh_memory_block(lines: list[str], block: list[str]) -> tuple[list[str], str, str]:
    """``(new lines, change note, refusal)`` for the Memory Stack block of CLAUDE.md."""
    begins = [i for i, line in enumerate(lines) if line.strip() == MARKER_BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == MARKER_END]
    if begins or ends:
        if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
            return (
                lines,
                "",
                "memory-stack markers are unbalanced or out of order; fix them by hand",
            )
        b, e = begins[0], ends[0]
        if [line.rstrip() for line in lines[b : e + 1]] == block:
            return lines, "", ""
        return (
            lines[:b] + block + lines[e + 1 :],
            f"L{b + 1}-L{e + 1} marker block -> current template block",
            "",
        )
    bounds = _heading_bounds(lines)
    if bounds is None:
        return (
            lines,
            "",
            f"no markers and no '{MEMORY_STACK_START}' .. '{MEMORY_STACK_END}' span; fix by hand",
        )
    start, end = bounds
    span = _normalize(lines[start:end])
    if span == _normalize(block):
        return lines, "", ""  # hand-fixed to the current text already; nothing stale to replace
    if not stale_memory_guidance_hits(span) or span not in {
        _normalize(v.splitlines()) for v in KNOWN_STALE_MEMORY_SPANS
    }:
        return (
            lines,
            "",
            (
                f"L{start + 1}-L{end} memory span (no markers) differs from every known template version "
                "(node-customized?); left untouched — replace it by hand"
            ),
        )
    new = lines[:start] + block + [""] + lines[end:]
    return (
        new,
        f"L{start + 1}-L{end} legacy memory span (known old template) -> current template block",
        "",
    )


def _join(lines: list[str], original: str) -> str:
    """Re-join edited lines keeping the file's newline style and final newline."""
    newline = "\r\n" if "\r\n" in original else "\n"
    text = newline.join(lines)
    return text + newline if original.endswith("\n") else text


def refresh_claude_md(text: str, template_text: str) -> Outcome:
    """Refresh the Memory Stack block and known stale lines of a node CLAUDE.md."""
    block = [line.rstrip() for line in template_block(template_text)]
    lines = text.splitlines()
    lines, note, refused = _refresh_memory_block(lines, block)
    if refused:
        return Outcome(text, refused=refused, remaining=stale_memory_guidance_hits(text))
    changes = [note] if note else []
    _replace_known_lines(lines, CLAUDE_KNOWN_LINES, template_text, changes)
    new_text = _join(lines, text) if changes else text
    return Outcome(new_text, changes=changes, remaining=stale_memory_guidance_hits(new_text))


def refresh_memory_md(text: str, template_text: str) -> Outcome:
    """Rewrite the memory-stack line of a MEMORY.md only when it equals the old template line."""
    lines = text.splitlines()
    changes: list[str] = []
    _replace_known_lines(lines, MEMORY_KNOWN_LINES, template_text, changes)
    new_text = _join(lines, text) if changes else text
    remaining = stale_memory_guidance_hits(new_text)
    if remaining and not changes:
        return Outcome(
            text,
            refused="stale line(s) do not match the known template line exactly; left untouched — fix by hand",
            remaining=remaining,
        )
    return Outcome(new_text, changes=changes, remaining=remaining)


def _describe_hits(hits: list[tuple[int, list[str]]]) -> str:
    return ", ".join(f"L{n}({'/'.join(labels)})" for n, labels in hits)


def _backup_path(path: Path, stamp: str) -> Path:
    candidate = path.with_name(f"{path.name}.bak-refresh-guidance-{stamp}")
    n = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = path.with_name(f"{path.name}.bak-refresh-guidance-{stamp}.{n}")
        n += 1
    return candidate


def _atomic_write(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@dataclass
class Target:
    name: str
    path: Path
    kind: str  # "claude" | "memory"


def refresh_targets(claude_dir: Path, hermes_dir: Path) -> list[Target]:
    """The node-owned files the doctor's stale-memory-guidance row scans."""
    return [
        Target("CLAUDE.md", claude_dir / "CLAUDE.md", "claude"),
        Target("memories/MEMORY.md", claude_dir / "memories" / "MEMORY.md", "memory"),
        Target("hermes:memories/MEMORY.md", hermes_dir / "memories" / "MEMORY.md", "memory"),
    ]


def _process(target: Target, templates: dict[str, str], dry_run: bool, stamp: str, out) -> bool:
    """Refresh one file; True when it needs no further operator action."""
    label = f"{target.name} ({target.path})"
    try:
        raw = target.path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"  refused   {label}: unreadable ({type(exc).__name__}); left untouched", file=out)
        return False
    refresh = refresh_claude_md if target.kind == "claude" else refresh_memory_md
    outcome = refresh(text, templates[target.kind])
    left = (
        f"; still stale, fix by hand: {_describe_hits(outcome.remaining)}"
        if outcome.remaining
        else ""
    )
    if outcome.refused:
        print(f"  refused   {label}: {outcome.refused}{left}", file=out)
        return False
    if outcome.text == text:
        print(f"  current   {label}: nothing to refresh{left}", file=out)
        return not outcome.remaining
    what = "; ".join(outcome.changes)
    if dry_run:
        print(f"  would     {label}: {what} (backup *.bak-refresh-guidance-<UTC>){left}", file=out)
        return not outcome.remaining
    backup = _backup_path(target.path, stamp)
    shutil.copy2(target.path, backup)
    _atomic_write(target.path, outcome.text)
    print(f"  refreshed {label}: {what}; backup {backup.name}{left}", file=out)
    return not outcome.remaining


def run_refresh(
    repo: Path, claude_dir: Path, hermes_dir: Path, *, dry_run: bool, out=sys.stdout
) -> int:
    """0 when every present file is current/refreshed and clean; 1 when any needs a hand fix."""
    try:
        claude_template = (repo / "claude" / "CLAUDE.md.template").read_text(encoding="utf-8")
        memory_template = (repo / "hermes" / "memories" / "MEMORY.template.md").read_text(
            encoding="utf-8"
        )
        template_block(claude_template)
    except (OSError, TemplateError) as exc:
        print(f"refresh-guidance: ERROR: {exc}", file=sys.stderr)
        return 2
    templates = {"claude": claude_template, "memory": memory_template}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(
        f"refresh-guidance{' (dry-run: nothing written)' if dry_run else ''} — ccc-node#2238",
        file=out,
    )
    ok = True
    seen: set[Path] = set()
    for target in refresh_targets(claude_dir, hermes_dir):
        if not target.path.is_file():
            print(f"  absent    {target.name} ({target.path})", file=out)
            continue
        real = target.path.resolve()
        if real in seen:
            print(
                f"  same-file {target.name} ({target.path}): already handled via its link target",
                file=out,
            )
            continue
        seen.add(real)
        try:
            ok = (
                _process(Target(target.name, real, target.kind), templates, dry_run, stamp, out)
                and ok
            )
        except (OSError, TemplateError) as exc:
            print(f"  refused   {target.name} ({target.path}): {exc}; left untouched", file=out)
            ok = False
    if not ok:
        print(
            "refresh-guidance: some guidance needs a hand fix (see 'refused' / 'still stale' above); "
            "re-check with scripts/ccc-doctor.sh (stale-memory-guidance)",
            file=out,
        )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--claude-dir", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--hermes-dir", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return run_refresh(args.repo, args.claude_dir, args.hermes_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
