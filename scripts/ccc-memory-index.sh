#!/usr/bin/env bash
# ccc-memory-index.sh — build/update the local hot-memory SQLite FTS5 index.
set -uo pipefail

STATE_DIR="${CCC_STATE_DIR:-/root/.claude/state}"
MEMORY_DIR="${CCC_MEMORY_DIR:-/root/.claude/memories}"
CACHE="${CCC_MEMORY_CACHE_DIR:-/root/.claude/hooks/cache}"
DB="${CCC_MEMORY_INDEX_DB:-$STATE_DIR/memory-index.sqlite}"
CMD="${1:-update}"

case "$CMD" in
  update|rebuild|check) ;;
  *) echo "usage: $0 [update|rebuild|check]" >&2; exit 2 ;;
esac

mkdir -p "$STATE_DIR"

python3 - "$CMD" "$DB" "$STATE_DIR" "$MEMORY_DIR" "$CACHE" <<'PY'
import json, os, sqlite3, sys
from pathlib import Path
cmd, db_path, state_dir, memory_dir, cache_dir = sys.argv[1:]
db = Path(db_path)
state = Path(state_dir)
mem = Path(memory_dir)
cache = Path(cache_dir)

SECRET_NAMES = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "ACCESS_KEY", "PRIVATE_KEY", "TELEGRAM_BOT_TOKEN")

def safe_text(text: str) -> str:
    lines=[]
    for line in text.splitlines():
        upper=line.upper()
        if any(name in upper for name in SECRET_NAMES) and "=" in line:
            continue
        lines.append(line)
    return "\n".join(lines).strip()

def read_json_text(path: Path) -> str:
    try:
        obj=json.loads(path.read_text(encoding='utf-8', errors='ignore'))
    except Exception:
        return safe_text(path.read_text(encoding='utf-8', errors='ignore'))
    parts=[]
    def walk(x):
        if isinstance(x, dict):
            for k,v in x.items():
                if any(s in str(k).upper() for s in SECRET_NAMES):
                    continue
                walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
        elif isinstance(x, (str,int,float,bool)) and x is not None:
            parts.append(str(x))
    walk(obj)
    return safe_text("\n".join(parts))

def docs():
    candidates=[]
    for name in ("MEMORY.md","USER.md"):
        candidates.append(("memory", mem/name))
    for name in ("wiki.txt","honcho.txt"):
        candidates.append(("cache", cache/name))
    for name in ("distill-last.json","wiki-candidates.md"):
        candidates.append(("state", state/name))
    hist=state/"distill-history"
    if hist.is_dir():
        for p in sorted(hist.glob("*.json"))[-200:]:
            candidates.append(("distill-history", p))
    for kind,p in candidates:
        if not p.is_file():
            continue
        if p.suffix == ".json":
            text=read_json_text(p)
        else:
            text=safe_text(p.read_text(encoding='utf-8', errors='ignore'))
        if text:
            yield kind, str(p), text

con=sqlite3.connect(db)
con.execute("CREATE TABLE IF NOT EXISTS memory_docs (source TEXT NOT NULL, path TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(path UNINDEXED, source UNINDEXED, content)")
if cmd == 'rebuild':
    con.execute("DELETE FROM memory_docs")
    con.execute("DELETE FROM memory_fts")
if cmd in ('update','rebuild'):
    for source,path,content in docs():
        con.execute("INSERT INTO memory_docs(source,path,content,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(path) DO UPDATE SET source=excluded.source, content=excluded.content, updated_at=CURRENT_TIMESTAMP", (source,path,content))
    con.execute("DELETE FROM memory_fts")
    con.execute("INSERT INTO memory_fts(path,source,content) SELECT path,source,content FROM memory_docs")
    con.commit()
count=con.execute("SELECT COUNT(*) FROM memory_docs").fetchone()[0]
print(json.dumps({"ok": True, "db": str(db), "documents": count}, ensure_ascii=False))
PY
