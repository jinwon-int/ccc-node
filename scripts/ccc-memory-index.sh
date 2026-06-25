#!/usr/bin/env bash
# ccc-memory-index.sh — build/update the local hot-memory SQLite FTS5 index.
# Privacy boundary: local-only cache, chmod 600 DB, best-effort redaction before indexing.
set -uo pipefail
umask 077

STATE_DIR="${CCC_STATE_DIR:-/root/.claude/state}"
MEMORY_DIR="${CCC_MEMORY_DIR:-/root/.claude/memories}"
CACHE="${CCC_MEMORY_CACHE_DIR:-/root/.claude/hooks/cache}"
DB="${CCC_MEMORY_INDEX_DB:-$STATE_DIR/memory-index.sqlite}"
CMD="${1:-update}"
INDEX_DISTILL="${CCC_MEMORY_INDEX_DISTILL:-0}"

case "$CMD" in
  update|rebuild|check) ;;
  *) echo "usage: $0 [update|rebuild|check]" >&2; exit 2 ;;
esac

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true

python3 - "$CMD" "$DB" "$STATE_DIR" "$MEMORY_DIR" "$CACHE" "$INDEX_DISTILL" <<'PY'
import json, os, re, sqlite3, sys
from pathlib import Path

cmd, db_path, state_dir, memory_dir, cache_dir, index_distill = sys.argv[1:]
os.umask(0o077)
db = Path(db_path)
state = Path(state_dir)
mem = Path(memory_dir)
cache = Path(cache_dir)
index_distill_enabled = index_distill.lower() in {"1", "true", "yes", "on"}

SECRET_NAMES = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY",
    "ACCESS_KEY", "PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "AUTHORIZATION",
    "COOKIE", "SESSION", "SIGNED_URL", "SIGNATURE",
)
SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|access[_-]?key|private[_-]?key|authorization|cookie|session|signature|signed[_-]?url)\b\s*[:=]\s*([^\s,'\"`]+)"
)
BEARER_RE = re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+|bearer\s+)([^\s,'\"`]+)")
URL_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|token|api_key|apikey|key|secret|password|sig|signature|x-tos-signature|x-amz-signature)=)([^&\s]+)"
)
PEM_RE = re.compile(
    r"-----BEGIN [^-]*(?:PRIVATE KEY|TOKEN|SECRET)[^-]*-----.*?-----END [^-]*(?:PRIVATE KEY|TOKEN|SECRET)[^-]*-----",
    re.IGNORECASE | re.DOTALL,
)


def redact_text(text: str) -> str:
    text = PEM_RE.sub("[REDACTED_PEM_SECRET]", text)
    text = URL_SECRET_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = BEARER_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    redacted_lines = []
    for line in text.splitlines():
        upper = line.upper()
        # Drop high-risk unstructured lines that mention a secret-like field and
        # contain a plausible value separator/path/url even if the exact key was unusual.
        if any(name in upper for name in SECRET_NAMES) and (
            ":" in line or "=" in line or "http://" in line or "https://" in line
        ):
            redacted_lines.append("[REDACTED_SENSITIVE_LINE]")
        else:
            redacted_lines.append(line)
    return "\n".join(redacted_lines).strip()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def read_json_text(path: Path) -> str:
    try:
        obj = json.loads(read_text(path))
    except Exception:
        return redact_text(read_text(path))
    parts = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if any(s in str(k).upper() for s in SECRET_NAMES):
                    parts.append(f"{k}=[REDACTED]")
                    continue
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, (str, int, float, bool)) and x is not None:
            parts.append(str(x))

    walk(obj)
    return redact_text("\n".join(parts))


def docs():
    candidates = []
    for name in ("MEMORY.md", "USER.md"):
        candidates.append(("memory", mem / name))
    for name in ("wiki.txt", "honcho.txt"):
        candidates.append(("cache", cache / name))

    # Distill artifacts can include raw transcript fragments. Keep them opt-in.
    if index_distill_enabled:
        for name in ("distill-last.json", "wiki-candidates.md"):
            candidates.append(("state", state / name))
        hist = state / "distill-history"
        if hist.is_dir():
            for p in sorted(hist.glob("*.json"))[-200:]:
                candidates.append(("distill-history", p))

    for kind, p in candidates:
        if not p.is_file():
            continue
        text = read_json_text(p) if p.suffix == ".json" else redact_text(read_text(p))
        if text:
            yield kind, str(p), text


def db_sidecars(path: Path):
    return [path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")]


def secure_db_files(path: Path):
    for p in db_sidecars(path):
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except OSError:
            pass


def remove_existing_db(path: Path):
    # Rebuild is a privacy boundary: old DB free pages may contain pre-redaction
    # plaintext. Delete the DB + sidecars before opening so the new file only
    # contains freshly redacted content.
    for p in db_sidecars(path):
        try:
            if p.exists():
                p.unlink()
        except FileNotFoundError:
            pass


db.parent.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(db.parent, 0o700)
except OSError:
    pass

if cmd == "rebuild":
    remove_existing_db(db)

con = sqlite3.connect(db)
try:
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA secure_delete=ON")
    con.execute("CREATE TABLE IF NOT EXISTS memory_docs (source TEXT NOT NULL, path TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(path UNINDEXED, source UNINDEXED, content)")
    if cmd == "rebuild":
        con.execute("DELETE FROM memory_docs")
        con.execute("DELETE FROM memory_fts")
    if cmd in ("update", "rebuild"):
        seen = set()
        for source, path, content in docs():
            seen.add(path)
            con.execute(
                "INSERT INTO memory_docs(source,path,content,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(path) DO UPDATE SET source=excluded.source, content=excluded.content, updated_at=CURRENT_TIMESTAMP",
                (source, path, content),
            )
        # Remove documents that disappeared or are no longer indexed under current policy.
        if seen:
            con.executemany(
                "DELETE FROM memory_docs WHERE path = ?",
                [(row[0],) for row in con.execute("SELECT path FROM memory_docs").fetchall() if row[0] not in seen],
            )
        else:
            con.execute("DELETE FROM memory_docs")
        con.execute("DELETE FROM memory_fts")
        con.execute("INSERT INTO memory_fts(path,source,content) SELECT path,source,content FROM memory_docs")
        con.commit()
        # Compact after update/rebuild so replaced/deleted plaintext from older
        # index versions is not left in SQLite free pages.
        con.execute("VACUUM")
    count = con.execute("SELECT COUNT(*) FROM memory_docs").fetchone()[0]
finally:
    con.close()
    secure_db_files(db)

print(json.dumps({"ok": True, "db": str(db), "documents": count, "distill_indexed": index_distill_enabled}, ensure_ascii=False))
PY
