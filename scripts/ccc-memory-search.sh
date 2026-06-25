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
import json, sqlite3, sys
path, query, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
con=sqlite3.connect(path)
rows=[]
try:
    cur=con.execute("SELECT path, source, snippet(memory_fts, 2, '[', ']', ' … ', 16) AS snippet, bm25(memory_fts) AS score FROM memory_fts WHERE memory_fts MATCH ? ORDER BY score LIMIT ?", (query, limit))
    for p,s,sn,score in cur:
        rows.append({"path":p,"source":s,"snippet":sn,"score":score})
except sqlite3.OperationalError:
    cur=con.execute("SELECT path, source, substr(content,1,240) FROM memory_docs WHERE content LIKE ? LIMIT ?", (f"%{query}%", limit))
    for p,s,sn in cur:
        rows.append({"path":p,"source":s,"snippet":sn,"score":None})
print(json.dumps({"query":query,"results":rows}, ensure_ascii=False, indent=2))
PY
