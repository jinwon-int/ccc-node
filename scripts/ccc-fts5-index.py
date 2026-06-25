#!/usr/bin/env python3
"""ccc-fts5-index — local SQLite FTS5 hot index for ccc-node memory.

Manages a full-text search index over ccc-node memory sources:
  - Built-in MEMORY.md / USER.md
  - Family Wiki cache
  - Honcho working memory cache

Subcommands:
  update   — rebuild the FTS5 index from current cache/memory files.
  search   — query the FTS5 index and print JSON matches.
  check    — health / statistics on the index.

Config:
  CCC_STATE_DIR      default /root/.claude/state          state/index root
  CCC_MEMORY_DIR     default /root/.claude/memories        MEMORY.md location
  CCC_MEMORY_CACHE_DIR default /root/.claude/hooks/cache   wiki+honcho caches

Security:
  This tool indexes plain-text memory/cache files ONLY. It never reads
  .env, honcho.json, .credentials.json, tokens, secrets, or key material.
  The index DB lives under CCC_STATE_DIR and holds the same
  non-secret content already in the source files.

Output:
  All subcommands write JSON to stdout.  Status / error messages go to stderr.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time


# ---------------------------------------------------------------------------
# defaults compatible with the existing root VPS convention
# ---------------------------------------------------------------------------

def _env_or(key: str, default: str) -> str:
    return os.environ.get(key, default)


STATE_DIR = _env_or("CCC_STATE_DIR", "/root/.claude/state")
MEM_DIR = _env_or("CCC_MEMORY_DIR", "/root/.claude/memories")
CACHE_DIR = _env_or("CCC_MEMORY_CACHE_DIR", "/root/.claude/hooks/cache")

DB_PATH = os.path.join(STATE_DIR, "ccc-fts5.db")

# files to index (checked for existence; secrets are excluded by pattern)
_SOURCES = [
    ("builtin-mem",  os.path.join(MEM_DIR, "MEMORY.md")),
    ("builtin-user", os.path.join(MEM_DIR, "USER.md")),
    ("wiki-cache",   os.path.join(CACHE_DIR, "wiki.txt")),
    ("honcho-cache", os.path.join(CACHE_DIR, "honcho.txt")),
]

# substrings that block a source from indexing (safety belt)
_SECRET_BLOCKLIST = [
    "Authorization: Bearer",
    "apiKey",
    "authToken",
    "SECRET",
    "PRIVATE KEY",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _emit(obj: dict) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _die(msg: str, exit_code: int = 1) -> None:
    _emit({"status": "error", "message": msg})
    sys.exit(exit_code)


def _db() -> sqlite3.Connection:
    os.makedirs(STATE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sources ("
        "  source TEXT NOT NULL PRIMARY KEY,"
        "  path TEXT NOT NULL,"
        "  sha256 TEXT NOT NULL,"
        "  indexed_at REAL NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
        "  source,"
        "  chunk,"
        "  content,"
        "  tokenize='porter unicode61'"
        ")"
    )
    conn.commit()


def _is_safe(content: str) -> bool:
    for token in _SECRET_BLOCKLIST:
        if token in content:
            return False
    return True


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_source(path: str) -> str | None:
    """Read a source file if it exists, is readable, and is safe."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = fh.read()
    except OSError:
        return None
    if not _is_safe(data):
        return None  # blocked by safety belt
    return data


def _needs_reindex(conn: sqlite3.Connection, source: str, path: str) -> bool:
    content = _read_source(path)
    if content is None:
        # source disappeared — remove old entries
        conn.execute("DELETE FROM memory_fts WHERE source=?", (source,))
        conn.execute("DELETE FROM sources WHERE source=?", (source,))
        conn.commit()
        return False
    h = _sha256(content)
    row = conn.execute("SELECT sha256 FROM sources WHERE source=?", (source,)).fetchone()
    if row is None or row[0] != h:
        return True
    return False


# ---------------------------------------------------------------------------
# subcommand: update
# ---------------------------------------------------------------------------

def cmd_update() -> None:
    started = time.time()
    conn = _db()
    _init_db(conn)

    indexed = []
    skipped = []
    total_chunks = 0

    for source, path in _SOURCES:
        content = _read_source(path)
        if content is None:
            skipped.append({"source": source, "reason": "missing_or_empty"})
            continue
        h = _sha256(content)

        row = conn.execute("SELECT sha256 FROM sources WHERE source=?", (source,)).fetchone()
        if row is not None and row[0] == h:
            skipped.append({"source": source, "reason": "unchanged"})
            continue

        # delete old chunks
        conn.execute("DELETE FROM memory_fts WHERE source=?", (source,))

        # chunk on double-newline boundaries, max 4000 chars per chunk
        chunks = [c.strip() for c in content.split("\n\n") if c.strip()]
        count = 0
        for chunk in chunks:
            if len(chunk) > 4000:
                chunk = chunk[:4000]
            conn.execute(
                "INSERT INTO memory_fts (source, chunk, content) VALUES (?, ?, ?)",
                (source, source, chunk),
            )
            count += 1

        conn.execute(
            "INSERT OR REPLACE INTO sources (source, path, sha256, indexed_at) VALUES (?, ?, ?, ?)",
            (source, path, h, time.time()),
        )
        total_chunks += count
        indexed.append({"source": source, "chunks": count, "sha256": h[:12]})

    conn.commit()
    elapsed = round(time.time() - started, 3)

    # vacuum if the DB grew noticeably (occasional, cheap with WAL)
    conn.execute("PRAGMA optimize")
    conn.close()

    _emit({
        "status": "ok",
        "indexed": indexed,
        "skipped": skipped,
        "total_chunks": total_chunks,
        "elapsed_s": elapsed,
        "db_path": DB_PATH,
    })


# ---------------------------------------------------------------------------
# subcommand: search
# ---------------------------------------------------------------------------

def cmd_search(query: str, limit: int = 10, raw: bool = False) -> None:
    conn = _db()
    _init_db(conn)

    # SQLite FTS5 MATCH with a prefix-star on the last token for prefix matches
    tokens = query.strip().split()
    if not tokens:
        _die("empty query")

    fts_query = " ".join(f"{t}*" for t in tokens)

    try:
        rows = conn.execute(
            "SELECT source, chunk, snippet(memory_fts, 2, '<b>', '</b>', '...', 40) AS snippet "
            "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        conn.close()
        _die(f"FTS5 query error: {exc}")

    conn.close()

    results = []
    for source, chunk_label, snippet in rows:
        results.append({
            "source": source,
            "chunk": chunk_label,
            "snippet": snippet,
        })

    _emit({
        "status": "ok",
        "query": query,
        "fts_query": fts_query,
        "count": len(results),
        "results": results,
    })


# ---------------------------------------------------------------------------
# subcommand: check
# ---------------------------------------------------------------------------

def cmd_check() -> None:
    conn = _db()
    _init_db(conn)

    # row counts
    src_rows = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    fts_rows = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]

    # per-source stats
    per_source = []
    for source, path in _SOURCES:
        row = conn.execute(
            "SELECT sha256, indexed_at FROM sources WHERE source=?", (source,)
        ).fetchone()
        chunk_count = conn.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE source=?", (source,)
        ).fetchone()[0]

        current = _read_source(path)
        stale = False
        current_sha = None
        if current is not None and row is not None:
            current_sha = _sha256(current)
            stale = current_sha != row[0]

        per_source.append({
            "source": source,
            "path": path,
            "chunks": chunk_count,
            "indexed_sha256": (row[0][:12] if row else None),
            "current_sha256": (current_sha[:12] if current_sha else None),
            "stale": stale,
            "indexed_at": (row[1] if row else None),
        })

    db_size = os.path.getsize(DB_PATH) if os.path.isfile(DB_PATH) else 0

    conn.close()

    _emit({
        "status": "ok",
        "db_path": DB_PATH,
        "db_size_bytes": db_size,
        "total_source_entries": src_rows,
        "total_fts_chunks": fts_rows,
        "sources": per_source,
    })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ccc-fts5-index — local FTS5 hot index for ccc-node memory"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # update
    sub.add_parser("update", help="rebuild FTS5 index from source files")

    # search
    p_search = sub.add_parser("search", help="query the FTS5 index")
    p_search.add_argument("query", help="search terms")
    p_search.add_argument("-n", "--limit", type=int, default=10, help="max results (default 10)")

    # check
    sub.add_parser("check", help="health / statistics of the FTS5 index")

    args = parser.parse_args()

    if args.command == "update":
        cmd_update()
    elif args.command == "search":
        cmd_search(args.query, args.limit)
    elif args.command == "check":
        cmd_check()


if __name__ == "__main__":
    main()
