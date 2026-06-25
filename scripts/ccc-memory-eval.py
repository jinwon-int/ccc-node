#!/usr/bin/env python3
"""ccc-memory-eval — offline eval harness for ccc-node local memory backends.

Measures:
  - latency (FTS5 update, search, check)
  - token budget (character output ranges)
  - refresh health (stale detection, source coverage)
  - recall quality (fixture-based precision/recall)

Uses FIXTURES ONLY — no live Honcho or provider calls. Outputs JSON.
All operations respect CCC_STATE_DIR and CCC_MEMORY_* env vars.

Usage:
  ccc-memory-eval.py                          # run all evals, output JSON
  ccc-memory-eval.py --suite latency          # latency-only
  ccc-memory-eval.py --suite recall           # recall-only
  ccc-memory-eval.py --fixtures-dir <dir>     # custom fixture dir
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------

def _env_or(k: str, d: str) -> str:
    return os.environ.get(k, d)


DEFAULT_FIXTURES_DIR = _env_or(
    "CCC_MEMORY_EVAL_FIXTURES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval-fixtures"),
)

# Which ccc-fts5-index.py to invoke
FTS5_INDEX = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ccc-fts5-index.py"
)


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------

FIXTURE_MANIFEST = {
    "sources": [
        {
            "source": "builtin-mem",
            "file": "MEMORY.md",
            "content": (
                "# MEMORY.md\n"
                "- This instance is eval-node / eval-cluster\n"
                "- Project uses Python 3.11 with uv for packaging\n"
                "- Memory stack: built-in + Honcho + Family Wiki + session_search\n"
                "- A2A fleet: T1 Seoseo broker, T2 Gwakga broker\n"
                "- GitHub repo hygiene: PR-first; squash merge after CI green\n"
            ),
        },
        {
            "source": "builtin-user",
            "file": "USER.md",
            "content": (
                "# USER.md\n"
                "- User is EvalUser, timezone UTC\n"
                "- Prefers concise technical responses\n"
                "- GitHub workflow: PR-first\n"
                "- Fresh approval needed for secrets, releases, DB migrations\n"
            ),
        },
        {
            "source": "wiki-cache",
            "file": "wiki.txt",
            "content": (
                "Family Wiki cache:\n"
                "- Node: eval-node, VPS1, Team1 worker\n"
                "- Gateway status: healthy\n"
                "- A2A Broker: seoseo-broker on port 4430\n"
                "- Wiki-first ops: consult Wiki before web/GitHub\n"
            ),
        },
        {
            "source": "honcho-cache",
            "file": "honcho.txt",
            "content": (
                "Honcho working memory:\n"
                "- User preference: concise technical responses\n"
                "- Current priority: ccc-node bootstrap\n"
                "- Seoyoon family ops: wiki-first, PR-first\n"
                "- Memory refresh: every session background\n"
            ),
        },
    ],
    "queries": [
        {
            "id": "q1",
            "query": "A2A broker",
            "expected_sources": ["wiki-cache"],
            "description": "Should find broker config in wiki cache",
        },
        {
            "id": "q2",
            "query": "Python uv packaging",
            "expected_sources": ["builtin-mem"],
            "description": "Should find tech stack in MEMORY.md",
        },
        {
            "id": "q3",
            "query": "GitHub PR workflow",
            "expected_sources": ["builtin-mem", "builtin-user"],
            "description": "Should find PR-first policy in multiple sources",
        },
        {
            "id": "q4",
            "query": "fresh approval secrets",
            "expected_sources": ["builtin-user"],
            "description": "Should find fresh-approval rules",
        },
        {
            "id": "q5",
            "query": "Seoyoon family wiki ops",
            "expected_sources": ["honcho-cache"],
            "description": "Should find Seoyoon ops in honcho cache",
        },
    ],
}


def _setup_fixtures(fixtures_dir: str) -> str:
    """Create fixture files in a temp dir and return the path."""
    workdir = tempfile.mkdtemp(prefix="ccc-eval-")

    mem_dir = os.path.join(workdir, "memories")
    cache_dir = os.path.join(workdir, "cache")
    state_dir = os.path.join(workdir, "state")

    for d in (mem_dir, cache_dir, state_dir):
        os.makedirs(d, exist_ok=True)

    for src in FIXTURE_MANIFEST["sources"]:
        dest = None
        if src["file"] == "MEMORY.md":
            dest = os.path.join(mem_dir, "MEMORY.md")
        elif src["file"] == "USER.md":
            dest = os.path.join(mem_dir, "USER.md")
        elif src["file"] == "wiki.txt":
            dest = os.path.join(cache_dir, "wiki.txt")
        elif src["file"] == "honcho.txt":
            dest = os.path.join(cache_dir, "honcho.txt")
        if dest:
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(src["content"])

    # copy user-supplied fixtures if available
    if os.path.isdir(fixtures_dir):
        for fname in os.listdir(fixtures_dir):
            src_path = os.path.join(fixtures_dir, fname)
            if not os.path.isfile(src_path):
                continue
            # override destination rules
            if fname == "MEMORY.md":
                dest = os.path.join(mem_dir, "MEMORY.md")
            elif fname == "USER.md":
                dest = os.path.join(mem_dir, "USER.md")
            elif fname == "wiki.txt":
                dest = os.path.join(cache_dir, "wiki.txt")
            elif fname == "honcho.txt":
                dest = os.path.join(cache_dir, "honcho.txt")
            else:
                continue
            with open(src_path, "r", encoding="utf-8") as fh:
                with open(dest, "w", encoding="utf-8") as fh_out:
                    fh_out.write(fh.read())

    # Write the workdir path for the harness to consume
    meta = {"mem_dir": mem_dir, "cache_dir": cache_dir, "state_dir": state_dir, "workdir": workdir}
    return meta


# ---------------------------------------------------------------------------
# eval: latency
# ---------------------------------------------------------------------------

def _run_fts5(meta, cmd, *args, timeout=30):
    env = {
        "CCC_STATE_DIR": meta["state_dir"],
        "CCC_MEMORY_DIR": meta["mem_dir"],
        "CCC_MEMORY_CACHE_DIR": meta["cache_dir"],
    }
    import subprocess
    full_cmd = [sys.executable, FTS5_INDEX, cmd] + list(args)
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            full_cmd, env={**os.environ, **env}, capture_output=True, text=True, timeout=timeout
        )
        elapsed = time.perf_counter() - t0
        if proc.returncode == 0:
            return json.loads(proc.stdout), elapsed, None
        else:
            return None, elapsed, f"exit {proc.returncode}: {proc.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return None, timeout, f"timeout after {timeout}s"


def latency_suite(meta) -> dict:
    results = {}

    # update latency
    t0 = time.perf_counter()
    result, elapsed, err = _run_fts5(meta, "update")
    results["update"] = {
        "latency_s": round(elapsed, 4),
        "indexed_sources": len(result.get("indexed", [])) if result else 0,
        "error": err,
    }

    # search latencies (warm)
    search_times = []
    for q in FIXTURE_MANIFEST["queries"]:
        result, elapsed, err = _run_fts5(meta, "search", q["query"], "-n", "5")
        search_times.append(elapsed)
    results["search"] = {
        "latency_min_s": round(min(search_times), 4),
        "latency_max_s": round(max(search_times), 4),
        "latency_mean_s": round(sum(search_times) / len(search_times), 4),
        "samples": len(search_times),
    }

    # check latency
    result, elapsed, err = _run_fts5(meta, "check")
    results["check"] = {
        "latency_s": round(elapsed, 4),
        "db_size_bytes": result.get("db_size_bytes", 0) if result else 0,
        "error": err,
    }

    return results


# ---------------------------------------------------------------------------
# eval: token budget
# ---------------------------------------------------------------------------

def token_budget_suite(meta) -> dict:
    # Ensure index is built
    _run_fts5(meta, "update")

    results = {}
    for q in FIXTURE_MANIFEST["queries"]:
        result, _, err = _run_fts5(meta, "search", q["query"], "-n", "5")
        if result is None:
            results[q["id"]] = {"error": err, "char_count": 0}
            continue
        raw = json.dumps(result)
        est_tokens = len(raw) // 4  # rough estimate: ~4 chars/token
        results[q["id"]] = {
            "result_count": result.get("count", 0),
            "json_bytes": len(raw),
            "est_tokens": est_tokens,
        }

    # total budget for a typical 5-result search
    total_bytes = sum(r.get("json_bytes", 0) for r in results.values() if isinstance(r, dict))
    total_tokens = total_bytes // 4
    return {
        "per_query": results,
        "total_json_bytes": total_bytes,
        "total_est_tokens": total_tokens,
    }


# ---------------------------------------------------------------------------
# eval: refresh health
# ---------------------------------------------------------------------------

def refresh_health_suite(meta) -> dict:
    # Ensure index is built
    _run_fts5(meta, "update")

    result, _, err = _run_fts5(meta, "check")
    if result is None:
        return {"error": err}

    sources = result.get("sources", [])
    stale_count = sum(1 for s in sources if s.get("stale"))
    total = len(sources)
    coverage = (total - stale_count) / total if total > 0 else 0

    per_source = []
    for s in sources:
        per_source.append({
            "source": s["source"],
            "chunks": s["chunks"],
            "stale": s["stale"],
            "has_indexed_sha": s["indexed_sha256"] is not None,
        })

    return {
        "total_sources": total,
        "stale_count": stale_count,
        "coverage": round(coverage, 3),
        "db_size_bytes": result.get("db_size_bytes", 0),
        "per_source": per_source,
    }


# ---------------------------------------------------------------------------
# eval: recall quality
# ---------------------------------------------------------------------------

def recall_quality_suite(meta) -> dict:
    # Ensure index is built before searching
    _run_fts5(meta, "update")

    per_query = []
    total_precision = 0.0
    total_recall = 0.0
    total_found = 0

    for q in FIXTURE_MANIFEST["queries"]:
        result, _, err = _run_fts5(meta, "search", q["query"], "-n", "10")
        if result is None:
            per_query.append({
                "id": q["id"],
                "error": err,
                "found": [],
                "expected": q["expected_sources"],
            })
            continue

        found_sources = [r["source"] for r in result.get("results", [])]
        expected = q["expected_sources"]

        # Precision: how many of found sources are in expected
        found_relevant = [s for s in found_sources if s in expected]
        precision = len(found_relevant) / len(found_sources) if found_sources else 0.0

        # Recall: how many of expected sources were found
        expected_found = [s for s in expected if s in found_sources]
        recall = len(expected_found) / len(expected) if expected else 0.0

        total_precision += precision
        total_recall += recall
        total_found += len(found_relevant)

        per_query.append({
            "id": q["id"],
            "query": q["query"],
            "expected": expected,
            "found": found_sources[:5],
            "precision": round(precision, 3),
            "recall": round(recall, 3),
        })

    n = len(FIXTURE_MANIFEST["queries"])
    return {
        "per_query": per_query,
        "mean_precision": round(total_precision / n, 3) if n > 0 else 0,
        "mean_recall": round(total_recall / n, 3) if n > 0 else 0,
        "total_relevant_found": total_found,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

SUITES = {
    "latency": latency_suite,
    "token-budget": token_budget_suite,
    "refresh-health": refresh_health_suite,
    "recall": recall_quality_suite,
}


def main():
    parser = argparse.ArgumentParser(description="ccc-memory-eval — offline eval harness")
    parser.add_argument(
        "--suite",
        nargs="*",
        choices=list(SUITES.keys()),
        help="eval suites to run (default: all)",
    )
    parser.add_argument(
        "--fixtures-dir",
        default=DEFAULT_FIXTURES_DIR,
        help="directory with fixture files",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="do not remove temp workdir after eval",
    )
    args = parser.parse_args()

    meta = _setup_fixtures(args.fixtures_dir)

    suites_to_run = args.suite if args.suite else list(SUITES.keys())

    report = {
        "eval_version": "1.0",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fixtures_dir": args.fixtures_dir,
        "state_dir": meta["state_dir"],
        "is_ci": os.environ.get("CI", "false").lower() in ("1", "true"),
    }

    has_error = False
    for suite_name in suites_to_run:
        fn = SUITES[suite_name]
        try:
            report[suite_name] = fn(meta)
        except Exception as exc:
            report[suite_name] = {"error": str(exc)}
            has_error = True

    # Summary
    summary = {}
    if "latency" in report:
        lt = report["latency"]
        summary["update_latency_s"] = lt.get("update", {}).get("latency_s", -1)
        search_lt = lt.get("search", {})
        summary["search_latency_mean_s"] = search_lt.get("latency_mean_s", -1)

    if "refresh-health" in report:
        rh = report["refresh-health"]
        summary["refresh_coverage"] = rh.get("coverage", -1)
        summary["refresh_stale_count"] = rh.get("stale_count", -1)

    if "recall" in report:
        rq = report["recall"]
        summary["mean_precision"] = rq.get("mean_precision", -1)
        summary["mean_recall"] = rq.get("mean_recall", -1)

    if "token-budget" in report:
        tb = report["token-budget"]
        summary["total_est_tokens"] = tb.get("total_est_tokens", -1)

    report["summary"] = summary

    # Cleanup (unless --keep-workdir)
    if not args.keep_workdir:
        shutil.rmtree(meta["workdir"], ignore_errors=True)
    else:
        report["workdir"] = meta["workdir"]

    report["exit_code"] = 1 if has_error else 0

    json.dump(report, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    sys.exit(report["exit_code"])


if __name__ == "__main__":
    main()
