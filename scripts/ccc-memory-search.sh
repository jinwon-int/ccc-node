#!/usr/bin/env bash
# ccc-memory-search.sh — query the local hot-memory SQLite index.
set -uo pipefail
STATE_DIR="${CCC_STATE_DIR:-/root/.claude/state}"
DB="${CCC_MEMORY_INDEX_DB:-$STATE_DIR/memory-index.sqlite}"
QUERY="${1:-}"
LIMIT="${CCC_MEMORY_SEARCH_LIMIT:-5}"
RETRIEVAL="${CCC_MEMORY_RETRIEVAL:-fts}"
[ -n "$QUERY" ] || { echo "usage: $0 <query>" >&2; exit 2; }
[ -f "$DB" ] || { echo "memory index missing: $DB" >&2; exit 1; }
python3 - "$DB" "$QUERY" "$LIMIT" "$RETRIEVAL" <<'PY'
import json, math, re, sqlite3, sys, time
from datetime import datetime, timezone
path, query, limit, retrieval = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
con=sqlite3.connect(path)
con.row_factory=sqlite3.Row

def tokens_for(q: str):
    # ccc-memory-query emits labels and paths. FTS5 MATCH treats ':'/'-'/'/' as
    # syntax, so fallback search uses useful tokens instead of raw punctuation.
    stop = {"task", "prompt", "node", "cwd", "issue", "pr", "git", "branch", "changed", "paths", "extra", "http", "https", "tmp", "root", "work"}
    toks=[]
    for t in re.findall(r"[0-9A-Za-z_가-힣]+", q):
        lo=t.lower()
        if len(t) < 2 or lo in stop:
            continue
        if lo not in toks:
            toks.append(lo)
    return toks[:12]

def fts_expr(toks):
    return " OR ".join('"%s"' % t.replace('"', '""') for t in toks)

def fts_query(toks):
    # Build a sanitized FTS5 MATCH from extracted tokens so it never trips over
    # the ':' / '-' / '/' punctuation in the task-aware query (which made the old
    # raw-query attempt throw and silently fall back to a flat token OR). Each
    # token is quoted (neutralizes FTS syntax); adjacent bigrams are added as
    # quoted phrases so documents where the terms appear *together* score higher
    # under bm25 — cheap proximity weighting without leaving FTS5.
    if not toks:
        return None
    parts = ['"%s"' % t.replace('"', '""') for t in toks]
    for a, b in zip(toks, toks[1:]):
        parts.append('"%s %s"' % (a.replace('"', '""'), b.replace('"', '""')))
    return " OR ".join(parts)

def run_fts(expr, n=None):
    # FTS5 candidate generation joined to memory_docs so the rerank can read
    # content/updated_at for the durability/recency/source signals.
    cur=con.execute(
        "SELECT f.path AS path, f.source AS source, "
        "snippet(memory_fts, 2, '[', ']', ' … ', 16) AS snippet, "
        "bm25(memory_fts) AS bm25, d.content AS content, d.updated_at AS updated_at "
        "FROM memory_fts f LEFT JOIN memory_docs d ON d.path = f.path "
        "WHERE memory_fts MATCH ? ORDER BY bm25 LIMIT ?",
        (expr, n or limit))
    return [{"path":r["path"],"source":r["source"],"snippet":r["snippet"],
             "bm25":r["bm25"],"content":r["content"] or "","updated_at":r["updated_at"] or ""} for r in cur]

def run_like(toks, n=None):
    if not toks:
        toks = [query.lower()]
    clauses = " OR ".join(["lower(content) LIKE ? OR lower(path) LIKE ?" for _ in toks])
    params=[]
    for t in toks:
        params.extend([f"%{t.lower()}%", f"%{t.lower()}%"])
    score_terms = " + ".join(["CASE WHEN lower(content) LIKE ? OR lower(path) LIKE ? THEN 1 ELSE 0 END" for _ in toks])
    sql = f"SELECT path, source, content, updated_at, ({score_terms}) AS hits FROM memory_docs WHERE {clauses} ORDER BY hits DESC, updated_at DESC LIMIT ?"
    cur=con.execute(sql, params + params + [n or limit])
    return [{"path":r["path"],"source":r["source"],"snippet":(r["content"] or "")[:240],
             "bm25":None,"token_hits":r["hits"],"content":r["content"] or "","updated_at":r["updated_at"] or ""} for r in cur]

def rerank(cands):
    # Re-rank FTS/LIKE candidates with the SAME explainable formula the hybrid
    # lane uses, so the DEFAULT retrieval path gets the source / recency /
    # durability signals too. Lexical relevance is distinct-token *coverage*
    # (term frequency is deliberately ignored) so a keyword-stuffed volatile or
    # review:rejected doc can no longer outrank a durable memory fact. A small
    # normalized bm25 term only breaks ties between equal-coverage candidates.
    if not cands:
        return []
    bms=[-(c["bm25"]) for c in cands if c.get("bm25") is not None]
    lo, hi = (min(bms), max(bms)) if bms else (0.0, 0.0)
    q=query.lower()
    out=[]
    for c in cands:
        content=c.get("content") or ""
        hay=(c["path"]+"\n"+c["source"]+"\n"+content).lower()
        token_hits=sum(1 for t in toks if t in hay)
        phrase_hit=1 if q and q in hay else 0
        if c.get("bm25") is not None and hi > lo:
            tie=((-c["bm25"]) - lo) / (hi - lo)
        else:
            tie=0.0
        signals={
            "token_hits": token_hits,
            "phrase_hit": phrase_hit,
            "source_boost": source_boost(c["source"], content),
            "recency_boost": round(recency_boost(c.get("updated_at")), 4),
            "durability_penalty": durability_penalty(content),
            "bm25_tiebreak": round(tie, 4),
        }
        if c.get("bm25") is not None:
            signals["fts_bm25"]=c["bm25"]
        score=(token_hits*4.0)+(phrase_hit*3.0)+signals["source_boost"]+signals["recency_boost"]+signals["durability_penalty"]+(tie*0.5)
        out.append({"path":c["path"],"source":c["source"],"snippet":c["snippet"],
                    "score":round(score,4),"signals":signals})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]

def source_boost(source: str, content: str):
    s=source or ""
    if s == "memory": return 3.0
    if s == "structured": return 2.5
    if s == "cache": return 1.5
    if s.startswith("distill"): return 0.8
    if s == "state": return 0.5
    return 1.0

def durability_penalty(content: str):
    c=content.lower()
    if "durability: volatile" in c or "kind: task-progress" in c:
        return -3.0
    if "review: rejected" in c:
        return -10.0
    return 0.0

def recency_boost(updated_at: str):
    try:
        dt=datetime.fromisoformat((updated_at or "").replace("Z","+00:00"))
        age=max(0.0, time.time()-dt.replace(tzinfo=timezone.utc).timestamp())
        return max(0.0, 1.0 - min(age, 7*86400)/(7*86400))
    except Exception:
        return 0.0

def hybrid(toks):
    rows=con.execute("SELECT path, source, content, updated_at FROM memory_docs").fetchall()
    q=query.lower()
    out=[]
    for r in rows:
        content=(r["content"] or "")
        hay=(r["path"]+"\n"+r["source"]+"\n"+content).lower()
        token_hits=sum(1 for t in toks if t in hay)
        phrase_hit=1 if q and q in hay else 0
        if token_hits == 0 and not phrase_hit:
            continue
        signals={
            "token_hits": token_hits,
            "phrase_hit": phrase_hit,
            "source_boost": source_boost(r["source"], content),
            "recency_boost": recency_boost(r["updated_at"]),
            "durability_penalty": durability_penalty(content),
        }
        # Simple local fusion. It is intentionally explainable and stdlib-only;
        # optional vector lanes can be added later without changing startup safety.
        score=(token_hits*4.0)+(phrase_hit*3.0)+signals["source_boost"]+signals["recency_boost"]+signals["durability_penalty"]
        sn=content[:240]
        out.append({"path":r["path"],"source":r["source"],"snippet":sn,"score":round(score,4),"signals":signals})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]

toks = tokens_for(query)
rows=[]
requested = retrieval.strip().lower() or "fts"
if requested in {"hybrid", "hybrid-local"}:
    mode = requested
    rows = hybrid(toks)
else:
    # Default: FTS5 candidate generation (over-fetched) + explainable boost
    # rerank. Falls back to LIKE (also reranked) when FTS5 is unavailable.
    mode = "fts-rerank"
    over = max(limit * 6, 30)
    expr = fts_query(toks)
    cands=[]
    if expr is not None:
        try:
            cands = run_fts(expr, over)
        except sqlite3.OperationalError:
            cands = []
    if not cands:
        try:
            cands = run_like(toks, over)
        except sqlite3.OperationalError:
            cands = []
    rows = rerank(cands)
print(json.dumps({"query":query,"tokens":toks,"retrievalMode":mode,"results":rows}, ensure_ascii=False, indent=2))
PY
