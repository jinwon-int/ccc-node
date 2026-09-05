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

import os
import sys

# json/math/pathlib/re/signal/subprocess are imported inside the subcommands
# that use them: this process is forked several times per SessionStart, and the
# hot subcommand (limit-bytes) needs none of them — eager imports taxed every
# call ~18ms of pure module-load time.


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


# ---------------------------------------------------------------------------
# Pure text transforms (#1484). Each subcommand below is a thin CLI wrapper
# around one of these so the `pipeline` subcommand can chain them in ONE
# interpreter with byte-identical results: every function returns exactly the
# bytes its CLI wrapper used to write to stdout, including the raw passthrough
# on malformed input.
# ---------------------------------------------------------------------------


def dedup_local_hot_text(raw, injected_text):
    """Return the deduped search JSON, or ``raw`` unchanged when not JSON."""
    import json
    import re

    try:
        doc = json.loads(raw)
    except Exception:
        return raw
    results = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(results, list) or not results:
        return raw

    def norm(t):
        return " ".join(re.findall(r"[0-9a-z가-힣]+", (t or "").lower()))

    injected = norm(injected_text)
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
    return json.dumps(doc, ensure_ascii=False)


def filter_disabled_wiki_hits_text(raw):
    """Return search JSON without wiki/distill-artifact rows (fails closed)."""
    import json
    import pathlib

    try:
        doc = json.loads(raw)
    except Exception:
        return '{"results":[]}'
    results = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(results, list):
        return '{"results":[]}'

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
    return json.dumps(doc, ensure_ascii=False)


def render_local_hot_text(raw):
    """Return compact "- (source) snippet" lines, or ``raw`` when not JSON."""
    import json
    import re

    try:
        doc = json.loads(raw)
    except Exception:
        return raw
    results = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(results, list):
        return raw
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
    return "\n".join(lines)


def merge_local_hot_text(primary, recent, shared, legacy, primary_audience):
    """Return the merged ``{"results": [...]}`` JSON for the four lanes."""
    import json

    def rows(raw):
        try:
            doc = json.loads(raw)
        except Exception:
            return []
        value = doc.get("results") if isinstance(doc, dict) else None
        return value if isinstance(value, list) else []

    if primary_audience not in {"private", "shared"}:
        primary_audience = "private"
    out, positions = [], {}
    for audience, raw in (
        (primary_audience, recent),
        (primary_audience, primary),
        ("shared", shared),
        ("private-legacy", legacy),
    ):
        for row in rows(raw):
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
    return json.dumps({"results": out}, ensure_ascii=False)


def dynamic_budget(total, reserve, maxlocal, bpr, base, maxlim, m, r, w):
    """Return ``(alloc, limit)`` — see cmd_dynamic_budget for the arithmetic."""
    alloc = max(maxlocal, total - reserve - m - r - w)
    return alloc, max(base, min(maxlim, alloc // bpr))


def _search_timeout(raw_timeout):
    import math

    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        timeout = 3.0
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = 3.0
    return min(timeout, 10.0)


def run_memory_search_bounded(tool, query, limit, raw_timeout, state_override, deadline=None):
    """Run the search tool under its deadline; return stdout bytes or None.

    ``None`` means "emit nothing" (nonzero exit, timeout, or unspawnable
    interpreter) — exactly the cases where the CLI wrapper prints nothing.
    ``deadline`` (a ``time.monotonic()`` instant) additionally caps the wait so
    concurrent audience lanes can share one global budget (#897 step 2).
    """
    import signal
    import subprocess
    import time

    timeout = _search_timeout(raw_timeout)
    if deadline is not None:
        timeout = max(0.0, min(timeout, deadline - time.monotonic()))
    env = os.environ.copy()
    env["CCC_MEMORY_RECORD_USAGE"] = "0"
    env["CCC_MEMORY_SEARCH_LIMIT"] = limit
    if state_override:
        env["CCC_STATE_DIR"] = state_override
        env["CCC_MEMORY_INDEX_DB"] = os.path.join(state_override, "memory-index.sqlite")
    try:
        proc = subprocess.Popen(
            ["bash", tool, query],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        sys.stderr.write(
            "memory_render: run-memory-search-bounded: cannot spawn tool %r: %s\n"
            % (tool, exc)
        )
        return None
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
        return None
    if proc.returncode == 0:
        return stdout
    return None


def cmd_dedup_local_hot(argv):
    """env: INJECTED (rendered canonical blocks), SEARCH_JSON; stdout: JSON.

    Cross-source injection dedup (see dedup_local_hot in load-memory.sh): drop a
    memory/cache hit only when its snippet is already fully present in the
    injected text, so anything truncated away from the canonical block is still
    kept (lossless). Structured (distilled-fact) and distill-state hits have no
    other injection path and are always kept.
    """
    sys.stdout.write(
        dedup_local_hot_text(os.environ.get("SEARCH_JSON", ""), os.environ.get("INJECTED", ""))
    )


def cmd_filter_disabled_wiki_hits(argv):
    """env: SEARCH_JSON; stdout: JSON with wiki/distill-artifact rows removed.

    Fail closed immediately when Wiki memory is disabled, even before the next
    background index update removes a stale wiki.txt row from SQLite.
    """
    sys.stdout.write(filter_disabled_wiki_hits_text(os.environ.get("SEARCH_JSON", "")))


def cmd_render_local_hot(argv):
    """env: SEARCH_JSON; stdout: compact "- (source) snippet" lines.

    The raw search JSON carries full filesystem paths, a per-result score and an
    8-field `signals` object that are debug-only noise to the model and waste
    the bounded injection budget — the agent only needs the snippet and which
    source it came from.
    """
    sys.stdout.write(render_local_hot_text(os.environ.get("SEARCH_JSON", "")))


def cmd_run_memory_search_bounded(argv):
    """argv: <tool> <query> <limit> <timeout-seconds> <state-dir-or-empty>.

    Bounded subprocess runner for ccc-memory-search: spawns the tool in its own
    session, enforces a hard deadline (clamped to 10s — the outer SessionStart
    hook has a 15-second deadline; keep enough room for canonical source
    assembly and JSON rendering even with an excessive override), and on timeout
    escalates SIGTERM -> SIGKILL against the whole process group. Emits the
    tool's stdout only on exit 0. Uses Python rather than GNU timeout so the
    same contract works on Termux.

    The tool is spawned through an explicit `bash` rather than its shebang:
    Termux has no /usr/bin/env, so exec'ing the script directly fails with
    ENOENT against the *interpreter* — and the OSError below used to swallow
    that silently, leaving hot-memory search permanently empty on that whole
    platform (#1159; same root as #472/#663/#1151/#1157). A missing *tool* is
    unchanged behavior-wise: bash starts, fails to open the script, exits 127,
    and no output is emitted. An OSError now means bash itself could not be
    spawned, which is genuinely exceptional — so it is noted on stderr (the
    hook's stderr lands in the session/hook logs) instead of vanishing.
    """
    tool, query, limit, raw_timeout, state_override = argv
    stdout = run_memory_search_bounded(tool, query, limit, raw_timeout, state_override)
    if stdout is not None:
        sys.stdout.buffer.write(stdout)


def cmd_merge_local_hot(argv):
    """Merge task/recent/shared/legacy result JSON with audience labels.

    ``PRIMARY_JSON`` and ``RECENT_JSON`` belong to ``PRIMARY_AUDIENCE``;
    the remaining sources are shared and private-legacy. Rows are deduped by
    document path (falling back to snippet when pathless), with the recent lane
    taking precedence, then re-sorted by score descending.
    """
    env = os.environ.get
    sys.stdout.write(
        merge_local_hot_text(
            env("PRIMARY_JSON", ""), env("RECENT_JSON", ""), env("SHARED_JSON", ""),
            env("LEGACY_JSON", ""), env("PRIMARY_AUDIENCE", "private"),
        )
    )


def cmd_dynamic_budget(argv):
    """argv: <total> <reserve> <maxlocal> <bytes-per-result> <base-limit>
    <max-limit> <mem-size> <resume-size> <wiki-size>.

    Relevance-aware budget arithmetic (see load-memory.sh): alloc = byte budget
    for the local hot block (>= maxlocal, reclaiming slack up to the total minus
    the scaffold reserve); the second number = how many results to fetch to fill
    it (~bpr bytes/result, clamped to [base, maxlim]). Prints "alloc limit".
    """
    alloc, limit = dynamic_budget(*(int(x) for x in argv))
    print(alloc, limit)


# ---------------------------------------------------------------------------
# pipeline (#1484): dynamic-budget -> bounded search lane(s) -> [merge] ->
# [filter-disabled-wiki-hits] -> dedup-local-hot -> render-local-hot, plus the
# pending-promises and detached-jobs blocks, in ONE interpreter start.
#
# load-memory.sh used to fork python3 once per arrow above (5-7 starts at
# ~25-40ms each on the SessionStart critical path) and passed the intermediate
# JSON through argv/env each time. The search tool itself is still a subprocess
# (it is a separate script with its own DB access) and scan-injection.sh stays
# a separate spawn AFTER this pipeline: it is the security boundary for every
# injected block and is deliberately not importable.
#
# Contract: argv is `key=value` words (see _pipeline_opts); env INJECTED is the
# dedup reference text exactly as dedup-local-hot takes it. Stdout is ONE line
# `alloc=<n> limit=<s> <stage>=<ms> ...`; the block bodies go to files under
# `out=<dir>` (local_hot, promises, detached) because they can exceed what a
# shell wants to carry through a pipe and must never be parsed out of a
# multi-section stream. Every stage keeps its own fail-open fallback from the
# shell glue it replaces: a broken stage degrades to the same bytes the old
# `|| printf ...` produced, never to a failed hook.
# ---------------------------------------------------------------------------


def _pipeline_opts(argv):
    opts = {}
    for word in argv:
        key, sep, value = word.partition("=")
        if sep:
            opts[key] = value
    return opts


def _write_block(out_dir, name, text):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write(text)


def _load_sibling(name):
    """Import a stdlib-only sibling helper (pending_promises / detached_jobs)."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location("_ccc_" + name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _promises_block(path, max_bytes):
    """Same bytes `pending_promises.py <path> --max-bytes N` writes to stdout."""
    try:
        pp = _load_sibling("pending_promises")
        body = pp.render(pp.load_records(path))
        if not body:
            return ""
        return pp.limit_bytes(body, pp._max_bytes(["--max-bytes", max_bytes])) + "\n"
    except Exception:
        return ""


def _detached_block(path, max_bytes):
    """Same bytes `detached_jobs.py sweep <path> --max-bytes N` writes."""
    try:
        dj = _load_sibling("detached_jobs")
        body = dj.render(dj.load_records(dj.registry_path(path)))
        if not body:
            return ""
        return dj.limit_bytes(body, dj._max_bytes(["--max-bytes", max_bytes])) + "\n"
    except Exception:
        return ""


def _pipeline_budget(opts):
    """(alloc, limit) — dynamic-budget with the standalone subcommand's
    fallback: any failure keeps the static floor and the limit as given."""
    alloc = opts.get("alloc", "")
    limit = opts.get("limit", "")
    budget = opts.get("budget", "")
    if budget:
        try:
            b_alloc, b_limit = dynamic_budget(*(int(x) for x in budget.split(",")))
            alloc = str(b_alloc)
            if not limit:
                limit = str(b_limit)
        except Exception:
            pass
    return alloc, limit


def _pipeline_lanes(opts, audience_scoped):
    """[(name, query, timeout, state_dir)] — the local lane, plus the audience
    lanes whose state dirs the caller resolved (empty dir == lane not wanted)."""
    tool = opts.get("tool", "")
    query = opts.get("query", "")
    state_dir = opts.get("state_dir", "")
    lanes = []
    if not tool:
        return lanes
    lanes.append(("local", query, opts.get("timeout", "3"), state_dir))
    if audience_scoped:
        lanes.append(("recent", "distilled text", opts.get("recent_timeout", "1"), state_dir))
        if opts.get("shared_state_dir", ""):
            lanes.append(("shared", query, opts.get("shared_timeout", "3"), opts["shared_state_dir"]))
        if opts.get("legacy_state_dir", ""):
            lanes.append(("legacy", query, opts.get("legacy_timeout", "2"), opts["legacy_state_dir"]))
    return lanes


def _pipeline_search(opts, lanes, limit, parallel):
    """Run the lanes; return ({name: text}, [(stage, ms)]).

    Parallel (audience) mode runs every lane concurrently under ONE global
    budget, each lane keeping its own inner timeout, and reports a single
    `search_parallel` stage; serial mode reports `search_<lane>` per lane.
    """
    import time

    tool = opts.get("tool", "")
    results, stages = {}, []

    def decode(raw):
        return "" if raw is None else raw.decode("utf-8", "surrogateescape")

    if parallel and len(lanes) > 1:
        import threading

        try:
            budget_sec = int(opts.get("global_timeout", "3"))
        except ValueError:
            budget_sec = 3
        t0 = time.monotonic()
        deadline = t0 + budget_sec

        def run(lane):
            name, q, tmo, sdir = lane
            results[name] = decode(run_memory_search_bounded(tool, q, limit, tmo, sdir, deadline))

        threads = [threading.Thread(target=run, args=(lane,), daemon=True) for lane in lanes]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        stages.append(("search_parallel", int((time.monotonic() - t0) * 1000)))
        return results, stages
    for name, q, tmo, sdir in lanes:
        t0 = time.monotonic()
        results[name] = decode(run_memory_search_bounded(tool, q, limit, tmo, sdir))
        stages.append(("search_" + name, int((time.monotonic() - t0) * 1000)))
    return results, stages


def _pipeline_post(local_hot, opts):
    """filter -> dedup -> render, each with the fallback its shell glue had."""
    on = lambda key: opts.get(key, "0") == "1"  # noqa: E731
    if not local_hot:
        # The old shell skipped the whole chain on "" and so does this.
        return local_hot
    if not on("wiki_enabled"):
        try:
            local_hot = filter_disabled_wiki_hits_text(local_hot)
        except Exception:
            local_hot = '{"results":[]}'
    if on("dedup"):
        try:
            local_hot = dedup_local_hot_text(local_hot, os.environ.get("INJECTED", ""))
        except Exception:
            pass
    if on("render"):
        try:
            local_hot = render_local_hot_text(local_hot)
        except Exception:
            pass
    return local_hot


def cmd_pipeline(argv):
    opts = _pipeline_opts(argv)
    out_dir = opts.get("out", "")
    if not out_dir or not os.path.isdir(out_dir):
        sys.stderr.write("memory_render: pipeline: out=<dir> is required\n")
        raise SystemExit(2)
    audience_scoped = opts.get("audience_scoped", "0") == "1"

    alloc, limit = _pipeline_budget(opts)
    lanes = _pipeline_lanes(opts, audience_scoped)
    results, stages = _pipeline_search(
        opts, lanes, limit, audience_scoped and opts.get("parallel", "0") == "1"
    )
    local_hot = results.get("local", "")
    if lanes and audience_scoped:
        try:
            local_hot = merge_local_hot_text(
                local_hot, results.get("recent", ""), results.get("shared", ""),
                results.get("legacy", ""), opts.get("audience", "private"),
            )
        except Exception:
            pass
    _write_block(out_dir, "local_hot", _pipeline_post(local_hot, opts))

    # The two silent-when-empty evidence blocks (#1258); an empty path means
    # the caller disabled the block or its module is not readable.
    promises = _promises_block(opts["promises_file"], opts.get("promises_max", "")) if opts.get("promises_file") else ""
    _write_block(out_dir, "promises", promises)
    detached = _detached_block(opts["detached_registry"], opts.get("detached_max", "")) if opts.get("detached_registry") else ""
    _write_block(out_dir, "detached", detached)

    sys.stdout.write("alloc=%s limit=%s" % (alloc, limit))
    for name, ms in stages:
        sys.stdout.write(" %s=%d" % (name, ms))
    sys.stdout.write("\n")


COMMANDS = {
    "limit-bytes": cmd_limit_bytes,
    "dedup-local-hot": cmd_dedup_local_hot,
    "filter-disabled-wiki-hits": cmd_filter_disabled_wiki_hits,
    "render-local-hot": cmd_render_local_hot,
    "run-memory-search-bounded": cmd_run_memory_search_bounded,
    "merge-local-hot": cmd_merge_local_hot,
    "dynamic-budget": cmd_dynamic_budget,
    "pipeline": cmd_pipeline,
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
