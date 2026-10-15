#!/usr/bin/env python3
"""gotgan (곳간) — nunchi's own verbatim transcript store (#2331).

A dependency-light replacement candidate for the MemPalace verbatim layer
(#816). stdlib sqlite3 only; numpy + model2vec are optional and, when absent,
search degrades to lexical-only instead of failing.

Index layout (one file, default ``$NUNCHI_HOME/gotgan.db``):
  chunks       one row per user/assistant message (tool results, hidden
               reasoning and <system-reminder> blocks are dropped)
  chunks_tri   FTS5 trigram over the body — ASCII/numbers/identifiers and any
               term of 3+ characters
  chunks_bi    FTS5 over Hangul character bigrams — a Korean term of any length
               >= 2 is matched as a bigram *phrase*, so 2-syllable words that a
               trigram index cannot see (e.g. "배정") still hit
  vecs         optional static-embedding vectors (model2vec), float16 BLOBs
  files        per-transcript byte offset for append-only incremental indexing

Search merges the lexical (and optional vector) rankings with reciprocal-rank
fusion plus a small recency boost. Output lines mirror the excerpt lines
nunchi.py already feeds to dialectic, so the caller is backend-agnostic.

Usage:
  gotgan.py index <claude|codex|piri> <transcript-dir> [--scope S]
  gotgan.py search <query> [-n N] [--scope S] [--json]
  gotgan.py status
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
MAX_BODY = 4000          # characters kept per message chunk
EXCERPT = 300            # characters shown around the best match
RRF_K = 60
RECENCY_DAYS = 90.0      # half-weight horizon for the recency boost
RECENCY_WEIGHT = 0.004   # small: a tie-breaker, never outranks a better match
PROVIDERS = ("claude", "codex", "piri")

_HANGUL_RUN = re.compile(r"[가-힣]+")
_TOKEN = re.compile(r"[A-Za-z0-9_.\-/#]+|[가-힣]+")
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_CMD_NOISE = re.compile(r"^\s*<(command-|local-command-|bash-|task-notification)", re.S)
_STOP = {
    "the", "a", "an", "of", "to", "in", "and", "or", "is", "are", "was", "were",
    "what", "which", "how", "when", "where", "who", "why", "did", "do", "does",
    "무엇", "어떻게", "어디", "어느", "언제", "누가", "왜", "그리고", "또는",
    "있다", "없다", "한다", "했다", "된다", "됐다", "기억나", "알려줘", "말해줘",
}
_PARTICLE = re.compile(
    r"(에서는|에서|으로|이라|이란|인가|인지|한다|했다|합니까|입니까"
    r"|은|는|이|가|을|를|의|에|로|와|과|도|만)$")


# --------------------------------------------------------------------------- db
def db_path():
    explicit = os.environ.get("GOTGAN_DB")
    if explicit:
        return explicit
    home = os.environ.get("NUNCHI_HOME") or os.path.expanduser("~/.nunchi")
    return os.path.join(home, "gotgan.db")


def connect(path=None):
    path = path or db_path()
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    old = os.umask(0o077)
    try:
        c = sqlite3.connect(path, timeout=30)
    finally:
        os.umask(old)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS files(
        path TEXT PRIMARY KEY, provider TEXT, size INTEGER, offset INTEGER,
        inode INTEGER, indexed_at REAL);
    CREATE TABLE IF NOT EXISTS chunks(
        id INTEGER PRIMARY KEY, provider TEXT, scope TEXT, session TEXT,
        path TEXT, seq INTEGER, ts TEXT, role TEXT, body TEXT,
        hash TEXT UNIQUE);
    CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
    CREATE INDEX IF NOT EXISTS chunks_scope ON chunks(scope);
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_tri USING fts5(
        body, content='chunks', content_rowid='id', tokenize='trigram');
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_bi USING fts5(grams);
    CREATE TABLE IF NOT EXISTS vecs(id INTEGER PRIMARY KEY, v BLOB);
    """)
    c.execute("INSERT OR IGNORE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return c


def bigrams(text):
    """Hangul runs → space-separated character bigrams (single syllables kept)."""
    out = []
    for run in _HANGUL_RUN.findall(text or ""):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return " ".join(out)


# ------------------------------------------------------------------- parsing
def _text_blocks(content, allowed):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return " ".join(
        b.get("text", "") for b in content
        if isinstance(b, dict) and b.get("type") in allowed and isinstance(b.get("text"), str))


def _clean(text):
    text = _REMINDER.sub("", text or "").strip()
    if not text or _CMD_NOISE.match(text):
        return ""
    return text[:MAX_BODY]


def parse_line(provider, d):
    """One JSONL record → (role, text, ts, session) or None.

    Only human-visible conversation text is kept: tool calls/results and
    hidden reasoning never enter the index (same exclusion as session-tail).
    """
    if not isinstance(d, dict):
        return None
    if provider == "claude":
        if d.get("type") not in ("user", "assistant") or d.get("isMeta"):
            return None
        msg = d.get("message") or {}
        if not isinstance(msg, dict):
            return None
        content = msg.get("content")
        if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        role = "USER" if d["type"] == "user" else "AGENT"
        text = _text_blocks(content, {"text"})
        return role, text, d.get("timestamp"), d.get("sessionId")
    if provider == "codex":
        item = d.get("payload") or {}
        if not isinstance(item, dict):
            return None
        if d.get("type") == "response_item" and item.get("type") == "message":
            role = {"user": "USER", "assistant": "AGENT"}.get(str(item.get("role")))
            if not role:
                return None
            text = _text_blocks(item.get("content"), {"input_text", "output_text"})
            if role == "USER" and text.rstrip().endswith("[nunchi-codex-feed-816]"):
                return None   # nunchi's own feed prompt, not conversation
            return role, text, d.get("timestamp"), None
        return None
    if provider == "piri":
        if d.get("type") != "message":
            return None
        item = d.get("message") or {}
        if not isinstance(item, dict):
            return None
        role = {"user": "USER", "assistant": "AGENT"}.get(str(item.get("role")))
        if not role:
            return None
        return role, _text_blocks(item.get("content"), {"text"}), d.get("timestamp"), None
    return None


def _safe_regular(path, root):
    """Owned regular file under root, no symlink on the way (session-tail rule)."""
    p = Path(path).absolute()
    if not p.is_relative_to(Path(root).absolute()):
        return None
    for part in [p, *p.parents]:
        if part.is_symlink():
            return None
    try:
        st = os.stat(p, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        return None
    return st


# ------------------------------------------------------------------- indexing
def _insert(c, provider, scope, session, path, seq, ts, role, body, embed_rows):
    h = hashlib.sha256(f"{path}\0{seq}\0{role}\0{body}".encode()).hexdigest()
    cur = c.execute(
        "INSERT OR IGNORE INTO chunks(provider,scope,session,path,seq,ts,role,body,hash)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (provider, scope, session, path, seq, ts, role, body, h))
    if cur.rowcount != 1:
        return 0
    rid = cur.lastrowid
    c.execute("INSERT INTO chunks_tri(rowid, body) VALUES(?,?)", (rid, body))
    grams = bigrams(body)
    if grams:
        c.execute("INSERT INTO chunks_bi(rowid, grams) VALUES(?,?)", (rid, grams))
    embed_rows.append((rid, body))
    return 1


def _drop_path(c, path):
    rows = [r[0] for r in c.execute("SELECT id FROM chunks WHERE path=?", (path,))]
    for rid in rows:
        body = c.execute("SELECT body FROM chunks WHERE id=?", (rid,)).fetchone()[0]
        c.execute("INSERT INTO chunks_tri(chunks_tri, rowid, body) VALUES('delete', ?, ?)",
                  (rid, body))
        c.execute("DELETE FROM chunks_bi WHERE rowid=?", (rid,))
        c.execute("DELETE FROM vecs WHERE id=?", (rid,))
    c.execute("DELETE FROM chunks WHERE path=?", (path,))


def index_file(c, provider, path, root, scope, embed_rows):
    st = _safe_regular(path, root)
    if st is None:
        return 0
    key = str(Path(path).absolute())
    row = c.execute("SELECT size, offset, inode FROM files WHERE path=?", (key,)).fetchone()
    offset = 0
    if row:
        _size, prev_off, inode = row
        if inode != st.st_ino or st.st_size < prev_off:
            _drop_path(c, key)          # rewritten/rotated → reindex from scratch
        else:
            offset = prev_off
    if offset == st.st_size and row:
        return 0
    seq0 = c.execute("SELECT COALESCE(MAX(seq), -1) FROM chunks WHERE path=?",
                     (key,)).fetchone()[0] + 1
    added = 0
    with open(key, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    # Only complete lines are consumed; a half-written tail waits for next run.
    end = data.rfind(b"\n")
    if end < 0:
        return 0
    consumed = data[:end + 1]
    session_default = Path(key).stem
    seq = seq0
    for raw in consumed.splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        parsed = parse_line(provider, d)
        if not parsed:
            continue
        role, text, ts, session = parsed
        body = _clean(text)
        if not body:
            continue
        added += _insert(c, provider, scope, session or session_default, key, seq,
                         ts, role, body, embed_rows)
        seq += 1
    c.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)",
              (key, provider, st.st_size, offset + len(consumed), st.st_ino, time.time()))
    return added


def index_dir(provider, root, scope="", embed=None):
    if provider not in PROVIDERS:
        raise SystemExit(f"provider must be one of {PROVIDERS}")
    root = os.path.abspath(os.path.expanduser(root))
    c = connect()
    embed = _embedder() if embed is None else embed
    total = files = 0
    for p in sorted(Path(root).rglob("*.jsonl")):
        embed_rows = []
        n = index_file(c, provider, p, root, scope, embed_rows)
        if n and embed:
            _store_vectors(c, embed, embed_rows)
        c.commit()
        total += n
        files += 1 if n else 0
    c.execute("INSERT OR REPLACE INTO meta VALUES('last_index', ?)",
              (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    c.commit()
    return {"provider": provider, "files_changed": files, "chunks_added": total}


# ------------------------------------------------------------- embeddings (opt)
def _embedder():
    """model2vec static model if installed and enabled, else None (lexical-only)."""
    if os.environ.get("GOTGAN_EMBED", "auto") == "off":
        return None
    try:
        import numpy  # noqa: F401
        from model2vec import StaticModel
    except Exception:
        return None
    name = os.environ.get("GOTGAN_EMBED_MODEL", "minishlab/potion-multilingual-128M")
    try:
        return StaticModel.from_pretrained(name)
    except Exception:
        return None


def _store_vectors(c, model, rows):
    import numpy as np
    if not rows:
        return
    vecs = model.encode([b for _, b in rows])
    vecs = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
    c.executemany("INSERT OR REPLACE INTO vecs VALUES(?,?)",
                  [(rid, v.astype("float16").tobytes()) for (rid, _), v in zip(rows, vecs)])


def _vector_rank(c, model, query, scope, limit):
    import numpy as np
    sql = "SELECT v.id, v.v FROM vecs v JOIN chunks k ON k.id=v.id"
    args = ()
    if scope:
        sql += " WHERE k.scope=?"
        args = (scope,)
    rows = c.execute(sql, args).fetchall()
    if not rows:
        return []
    ids = np.array([r[0] for r in rows])
    mat = np.frombuffer(b"".join(r[1] for r in rows), dtype="float16").reshape(len(rows), -1)
    q = model.encode([query])[0]
    q = q / (np.linalg.norm(q) + 1e-9)
    sims = mat.astype("float32") @ q.astype("float32")
    top = np.argsort(-sims)[:limit]
    return [int(ids[i]) for i in top]


# ------------------------------------------------------------------- search
def terms(query, max_terms=8):
    out = []
    for tok in _TOKEN.findall(query or ""):
        base = _PARTICLE.sub("", tok) if _HANGUL_RUN.fullmatch(tok) else tok
        if base.lower() in _STOP or base in _STOP or base in out:
            continue
        if _HANGUL_RUN.fullmatch(base):
            if len(base) >= 2:
                out.append(base)
        elif len(base) >= 3:
            out.append(base)
    return out[:max_terms]


def _fts_quote(s):
    return '"' + s.replace('"', '""') + '"'


def _lex_rank(c, query, scope, limit):
    ts = terms(query)
    hangul = [t for t in ts if _HANGUL_RUN.fullmatch(t)]
    other = [t for t in ts if not _HANGUL_RUN.fullmatch(t)]
    ranks = []
    scope_sql = " AND k.scope=?" if scope else ""
    scope_arg = (scope,) if scope else ()
    if other:
        q = " OR ".join(_fts_quote(t) for t in other)
        ranks.append([r[0] for r in c.execute(
            "SELECT t.rowid FROM chunks_tri t JOIN chunks k ON k.id=t.rowid"
            f" WHERE chunks_tri MATCH ?{scope_sql} ORDER BY bm25(chunks_tri) LIMIT ?",
            (q, *scope_arg, limit))])
    if hangul:
        q = " OR ".join(_fts_quote(bigrams(t)) for t in hangul)
        ranks.append([r[0] for r in c.execute(
            "SELECT b.rowid FROM chunks_bi b JOIN chunks k ON k.id=b.rowid"
            f" WHERE chunks_bi MATCH ?{scope_sql} ORDER BY bm25(chunks_bi) LIMIT ?",
            (q, *scope_arg, limit))])
    return ranks, ts


def _recency(ts):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
        return RECENCY_WEIGHT * (1.0 / (1.0 + max(age, 0.0) / RECENCY_DAYS))
    except (TypeError, ValueError):
        return 0.0


def _excerpt(body, ts):
    low = body.lower()
    pos = -1
    for t in ts:
        pos = low.find(t.lower())
        if pos >= 0:
            break
    start = max(0, (pos if pos >= 0 else 0) - EXCERPT // 3)
    piece = body[start:start + EXCERPT].replace("\n", " ")
    return ("…" if start else "") + piece + ("…" if start + EXCERPT < len(body) else "")


def search(query, n=3, scope="", path=None, embed=None):
    if not os.path.exists(path or db_path()):
        return []
    c = connect(path)
    limit = max(n * 10, 30)
    ranks, ts = _lex_rank(c, query, scope, limit)
    model = _embedder() if embed is None else embed
    if model and c.execute("SELECT 1 FROM vecs LIMIT 1").fetchone():
        ranks.append(_vector_rank(c, model, query, scope, limit))
    scores = {}
    for rank in ranks:
        for i, rid in enumerate(rank):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + i + 1)
    if not scores:
        return []
    rows = {r[0]: r for r in c.execute(
        f"SELECT id, provider, session, ts, role, body FROM chunks WHERE id IN "
        f"({','.join('?' * len(scores))})", tuple(scores))}
    for rid, r in rows.items():
        scores[rid] += _recency(r[3])
    out = []
    for rid in sorted(scores, key=lambda k: -scores[k])[:n]:
        _id, provider, session, t, role, body = rows[rid]
        out.append({"id": rid, "provider": provider, "session": session, "ts": t,
                    "role": role, "score": round(scores[rid], 5),
                    "excerpt": _excerpt(body, ts)})
    return out


def format_lines(hits):
    return [f"[{h['provider']} {str(h['ts'] or '')[:10]}] {h['role']}: {h['excerpt']}"
            for h in hits]


def status(path=None):
    p = path or db_path()
    if not os.path.exists(p):
        return {"state": "absent", "db": p}
    c = connect(p)
    meta = dict(c.execute("SELECT k, v FROM meta"))
    return {
        "state": "ok", "db": p, "bytes": os.path.getsize(p),
        "chunks": c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "vectors": c.execute("SELECT COUNT(*) FROM vecs").fetchone()[0],
        "files": c.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "by_provider": dict(c.execute("SELECT provider, COUNT(*) FROM chunks GROUP BY provider")),
        "integrity": c.execute("PRAGMA quick_check").fetchone()[0],
        "fts_rows": c.execute("SELECT COUNT(*) FROM chunks_tri").fetchone()[0],
        "last_index": meta.get("last_index"), "schema": meta.get("schema"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gotgan")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("index")
    a.add_argument("provider")
    a.add_argument("dir")
    a.add_argument("--scope", default="")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("-n", type=int, default=3)
    s.add_argument("--scope", default="")
    s.add_argument("--json", action="store_true")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    if args.cmd == "index":
        print(json.dumps(index_dir(args.provider, args.dir, args.scope), ensure_ascii=False))
    elif args.cmd == "search":
        hits = search(args.query, args.n, args.scope)
        if args.json:
            print(json.dumps(hits, ensure_ascii=False))
        else:
            print("\n".join(format_lines(hits)))
    else:
        print(json.dumps(status(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
