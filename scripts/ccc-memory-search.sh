#!/usr/bin/env bash
# ccc-memory-search.sh — query the local hot-memory SQLite FTS5 index.
set -uo pipefail
STATE_DIR="${CCC_STATE_DIR:-/root/.claude/state}"
DB="${CCC_MEMORY_INDEX_DB:-$STATE_DIR/memory-index.sqlite}"
QUERY="${1:-}"
LIMIT="${CCC_MEMORY_SEARCH_LIMIT:-5}"
[ -n "$QUERY" ] || { echo "usage: $0 <query>" >&2; exit 2; }
[ -f "$DB" ] || { echo "memory index missing: $DB" >&2; exit 1; }
python3 - "$DB" "$QUERY" "$LIMIT" <<'PY'
import json, re, sqlite3, sys
path, query, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
con=sqlite3.connect(path)
rows=[]

def tokens_for(q: str):
    # ccc-memory-query emits human-readable field labels and paths. FTS5 MATCH
    # treats ':'/'-'/'/' as syntax, so search on useful tokens instead of the raw
    # task query when punctuation is present.
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

def run_fts(expr):
    cur=con.execute("SELECT path, source, snippet(memory_fts, 2, '[', ']', ' … ', 16) AS snippet, bm25(memory_fts) AS score FROM memory_fts WHERE memory_fts MATCH ? ORDER BY score LIMIT ?", (expr, limit))
    return [{"path":p,"source":s,"snippet":sn,"score":score} for p,s,sn,score in cur]

def run_like(toks):
    if not toks:
        toks = [query]
    clauses = " OR ".join(["lower(content) LIKE ?" for _ in toks])
    params = [f"%{t.lower()}%" for t in toks]
    sql = f"SELECT path, source, substr(content,1,240), ({' + '.join(['CASE WHEN lower(content) LIKE ? THEN 1 ELSE 0 END' for _ in toks])}) AS hits FROM memory_docs WHERE {clauses} ORDER BY hits DESC, updated_at DESC LIMIT ?"
    cur=con.execute(sql, params + params + [limit])
    return [{"path":p,"source":s,"snippet":sn,"score":None,"token_hits":hits} for p,s,sn,hits in cur]

toks = tokens_for(query)
try:
    try:
        rows = run_fts(query)
    except sqlite3.OperationalError:
        rows = []
    if not rows and toks:
        rows = run_fts(fts_expr(toks))
except sqlite3.OperationalError:
    rows = run_like(toks)
if not rows:
    rows = run_like(toks)
print(json.dumps({"query":query,"tokens":toks,"results":rows}, ensure_ascii=False, indent=2))
PY
