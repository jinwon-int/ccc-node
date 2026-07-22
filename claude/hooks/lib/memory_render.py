#!/usr/bin/env python3
"""Memory-injection rendering helpers for load-memory.sh (#584 P2-1).

Each subcommand is a former inline python3 heredoc from load-memory.sh, moved
here verbatim so the shell hook stays a thin orchestrator. Contracts (stdin /
env / argv / stdout / exit codes) are preserved exactly: the shell callers keep
their `|| printf ...` fail-open fallbacks, so any failure here (including this
file being missing on a node) degrades identically to a heredoc failure.

Stdlib-only and standalone by design — this deploys to ~/.claude/hooks/lib/
with no access to the repo's Python packages.

Usage: python3 memory_render.py <subcommand> [args...]
"""

import json
import math
import os
import pathlib
import re
import signal
import subprocess
import sys


def cmd_limit_bytes(argv):
    """stdin: raw bytes; argv: <max>; stdout: bytes capped at <max>."""
    limit = int(argv[0])
    data = sys.stdin.buffer.read()
    if limit > 0 and len(data) > limit:
        # Reserve room for the truncation marker so the total output stays within
        # <limit> bytes. Slicing to <limit> and THEN appending the suffix used to
        # overshoot the declared cap by the suffix length (~38 bytes).
        suffix = "\n… [truncated by CCC memory budget]\n".encode("utf-8")
        keep = max(0, limit - len(suffix))
        text = data[:keep].decode("utf-8", errors="ignore")
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.write(suffix)
    else:
        sys.stdout.buffer.write(data)


def cmd_dedup_local_hot(argv):
    """env: INJECTED (rendered canonical blocks), SEARCH_JSON; stdout: JSON.

    Cross-source injection dedup (see dedup_local_hot in load-memory.sh): drop a
    memory/cache hit only when its snippet is already fully present in the
    injected text, so anything truncated away from the canonical block is still
    kept (lossless). Structured (distilled-fact) and distill-state hits have no
    other injection path and are always kept.
    """
    raw = os.environ.get("SEARCH_JSON", "")
    try:
        doc = json.loads(raw)
    except Exception:
        sys.stdout.write(raw)
        sys.exit(0)
    results = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(results, list) or not results:
        sys.stdout.write(raw)
        sys.exit(0)

    def norm(t):
        return " ".join(re.findall(r"[0-9a-z가-힣]+", (t or "").lower()))

    injected = norm(os.environ.get("INJECTED", ""))
    kept, dropped = [], 0
    for r in results:
        if str(r.get("source") or "") not in ("memory", "cache"):
            kept.append(r)
            continue
        snip = str(r.get("snippet") or r.get("content") or r.get("text") or "")
        snip = snip.replace("[", " ").replace("]", " ")
        frags = [f for f in (norm(p) for p in re.split(r"\s*(?:…|\.\.\.)\s*", snip)) if len(f) >= 12]
        if injected and frags and all(f in injected for f in frags):
            dropped += 1
            continue
        kept.append(r)
    doc["results"] = kept
    if dropped:
        doc["injectionDedup"] = {"dropped": dropped, "kept": len(kept)}
    sys.stdout.write(json.dumps(doc, ensure_ascii=False))


def cmd_filter_disabled_wiki_hits(argv):
    """env: SEARCH_JSON; stdout: JSON with wiki/distill-artifact rows removed.

    Fail closed immediately when Wiki memory is disabled, even before the next
    background index update removes a stale wiki.txt row from SQLite.
    """
    raw = os.environ.get("SEARCH_JSON", "")
    try:
        doc = json.loads(raw)
    except Exception:
        sys.stdout.write('{"results":[]}')
        raise SystemExit(0)
    results = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(results, list):
        sys.stdout.write('{"results":[]}')
        raise SystemExit(0)

    def visible(row):
        if not isinstance(row, dict):
            return False
        p = pathlib.PurePath(str(row.get("path") or ""))
        source = str(row.get("source") or "").lower()
        if p.name in {"wiki.txt", "wiki-candidates.md"}:
            return False
        if source == "distill-local":
            return True
        return not (p.name == "distill-last.json" or "distill-history" in p.parts or source.startswith("distill"))

    doc["results"] = [row for row in results if visible(row)]
    sys.stdout.write(json.dumps(doc, ensure_ascii=False))


def cmd_render_local_hot(argv):
    """env: SEARCH_JSON; stdout: compact "- (source) snippet" lines.

    The raw search JSON carries full filesystem paths, a per-result score and an
    8-field `signals` object that are debug-only noise to the model and waste
    the bounded injection budget — the agent only needs the snippet and which
    source it came from.
    """
    raw = os.environ.get("SEARCH_JSON", "")
    try:
        doc = json.loads(raw)
    except Exception:
        sys.stdout.write(raw)
        sys.exit(0)
    results = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(results, list):
        sys.stdout.write(raw)
        sys.exit(0)
    LABEL = {"memory": "memory", "cache": "cache", "structured": "fact",
             "state": "distill", "distill-history": "distill", "distill-local": "distill"}
    lines = []
    for r in results:
        if not isinstance(r, dict):
            continue
        snip = str(r.get("snippet") or r.get("content") or r.get("text") or "")
        snip = re.sub(r"\s+", " ", snip.replace("[", "").replace("]", "")).strip()
        # FTS snippets bracket matches and wrap gaps in "…"; drop the leading/trailing
        # ellipsis so the rendered line reads cleanly (internal gaps are kept).
        snip = re.sub(r"^\s*(?:…|\.\.\.)\s*|\s*(?:…|\.\.\.)\s*$", "", snip)
        if not snip:
            continue
        lines.append(f"- ({LABEL.get(str(r.get('source') or ''), 'memory')}) {snip}")
    sys.stdout.write("\n".join(lines))


def cmd_run_memory_search_bounded(argv):
    """argv: <tool> <query> <limit> <timeout-seconds> <state-dir-or-empty>.

    Bounded subprocess runner for ccc-memory-search: spawns the tool in its own
    session, enforces a hard deadline (clamped to 10s — the outer SessionStart
    hook has a 15-second deadline; keep enough room for canonical source
    assembly and JSON rendering even with an excessive override), and on timeout
    escalates SIGTERM -> SIGKILL against the whole process group. Emits the
    tool's stdout only on exit 0. Uses Python rather than GNU timeout so the
    same contract works on Termux.
    """
    tool, query, limit, raw_timeout, state_override = argv
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        timeout = 3.0
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = 3.0
    timeout = min(timeout, 10.0)
    env = os.environ.copy()
    env["CCC_MEMORY_RECORD_USAGE"] = "0"
    env["CCC_MEMORY_SEARCH_LIMIT"] = limit
    if state_override:
        env["CCC_STATE_DIR"] = state_override
        env["CCC_MEMORY_INDEX_DB"] = os.path.join(state_override, "memory-index.sqlite")
    try:
        proc = subprocess.Popen(
            [tool, query],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError:
        raise SystemExit(0)
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()
        try:
            proc.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.communicate()
        raise SystemExit(0)
    if proc.returncode == 0:
        sys.stdout.buffer.write(stdout)


def cmd_merge_local_hot(argv):
    """Merge task/recent/shared/legacy result JSON with audience labels.

    ``PRIMARY_JSON`` and ``RECENT_JSON`` belong to ``PRIMARY_AUDIENCE``;
    the remaining sources are shared and private-legacy. Rows are deduped by
    document path (falling back to snippet when pathless), with the recent lane
    taking precedence, then re-sorted by score descending.
    """
    def rows(name):
        try:
            doc = json.loads(os.environ.get(name, ""))
        except Exception:
            return []
        value = doc.get("results") if isinstance(doc, dict) else None
        return value if isinstance(value, list) else []

    primary_audience = os.environ.get("PRIMARY_AUDIENCE", "private")
    if primary_audience not in {"private", "shared"}:
        primary_audience = "private"
    out, positions = [], {}
    for audience, name in (
        (primary_audience, "RECENT_JSON"),
        (primary_audience, "PRIMARY_JSON"),
        ("shared", "SHARED_JSON"),
        ("private-legacy", "LEGACY_JSON"),
    ):
        for row in rows(name):
            if not isinstance(row, dict):
                continue
            path = str(row.get("path") or "")
            snippet = str(row.get("snippet") or "")
            key = ("path", path) if path else ("snippet", snippet)
            if key in positions:
                existing = out[positions[key]]
                try:
                    existing["score"] = max(
                        float(existing.get("score") or 0),
                        float(row.get("score") or 0),
                    )
                except (TypeError, ValueError):
                    pass
                continue
            positions[key] = len(out)
            item = dict(row)
            item["memoryAudience"] = audience
            out.append(item)
    out.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
    sys.stdout.write(json.dumps({"results": out}, ensure_ascii=False))


def cmd_dynamic_budget(argv):
    """argv: <total> <reserve> <maxlocal> <bytes-per-result> <base-limit>
    <max-limit> <mem-size> <resume-size> <wiki-size> <honcho-size>.

    Relevance-aware budget arithmetic (see load-memory.sh): alloc = byte budget
    for the local hot block (>= maxlocal, reclaiming slack up to the total minus
    the scaffold reserve); the second number = how many results to fetch to fill
    it (~bpr bytes/result, clamped to [base, maxlim]). Prints "alloc limit".
    """
    total, reserve, maxlocal, bpr, base, maxlim, m, r, w, h = (int(x) for x in argv)
    alloc = max(maxlocal, total - reserve - m - r - w - h)
    print(alloc, max(base, min(maxlim, alloc // bpr)))


COMMANDS = {
    "limit-bytes": cmd_limit_bytes,
    "dedup-local-hot": cmd_dedup_local_hot,
    "filter-disabled-wiki-hits": cmd_filter_disabled_wiki_hits,
    "render-local-hot": cmd_render_local_hot,
    "run-memory-search-bounded": cmd_run_memory_search_bounded,
    "merge-local-hot": cmd_merge_local_hot,
    "dynamic-budget": cmd_dynamic_budget,
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write(
            "usage: memory_render.py {%s} [args...]\n" % "|".join(sorted(COMMANDS))
        )
        return 2
    COMMANDS[argv[0]](argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
