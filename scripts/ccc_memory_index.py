#!/usr/bin/env python3
# ruff: noqa: E401,E402,E701,E702
"""Build/update the local hot-memory SQLite FTS5 index."""

import os
import sys
from pathlib import Path

STATE_DIR = os.environ.get("CCC_STATE_DIR") or f"{os.environ.get('HOME', '/root')}/.claude/state"
MEMORY_DIR = os.environ.get("CCC_MEMORY_DIR") or f"{os.environ.get('HOME', '/root')}/.claude/memories"
CACHE = os.environ.get("CCC_MEMORY_CACHE_DIR") or f"{os.environ.get('HOME', '/root')}/.claude/hooks/cache"
DB = os.environ.get("CCC_MEMORY_INDEX_DB") or f"{STATE_DIR}/memory-index.sqlite"
CMD = sys.argv[1] if len(sys.argv) > 1 else "update"
INDEX_DISTILL = os.environ.get("CCC_MEMORY_INDEX_DISTILL") or "0"
FACTS_FILE = os.environ.get("CCC_MEMORY_FACTS_FILE") or f"{STATE_DIR}/memory-facts.jsonl"

if CMD not in {"update", "rebuild", "check"}:
    print(f"usage: {Path(sys.argv[0]).name} [update|rebuild|check]", file=sys.stderr)
    raise SystemExit(2)

Path(STATE_DIR).mkdir(parents=True, exist_ok=True)
try:
    os.chmod(STATE_DIR, 0o700)
except OSError:
    pass

sys.argv = [sys.argv[0], CMD, DB, STATE_DIR, MEMORY_DIR, CACHE, INDEX_DISTILL, FACTS_FILE]

import hashlib, json, os, re, sqlite3, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

cmd, db_path, state_dir, memory_dir, cache_dir, index_distill, facts_file_arg = sys.argv[1:]
os.umask(0o077)

# Optional, operator-configured embedding provider for the semantic retrieval
# lane. CCC_MEMORY_EMBED_CMD is a shell command that reads text on stdin and
# prints a JSON float array (or {"embedding":[...]}) on stdout — the repo ships
# NO provider/key, only the wiring. Doc embeddings are precomputed here (during
# the background refresh/index, where network is allowed) over the ALREADY
# REDACTED memory_docs content, so no secrets leave the node and SessionStart
# stays no-network. Unset by default → no embedding, no behavior change.
EMBED_CMD = os.environ.get("CCC_MEMORY_EMBED_CMD", "").strip()
EMBED_MODEL = os.environ.get("CCC_MEMORY_EMBED_MODEL", "").strip()
try:
    EMBED_TIMEOUT = float(os.environ.get("CCC_MEMORY_EMBED_TIMEOUT", "15") or 15)
except ValueError:
    EMBED_TIMEOUT = 15.0

def embed_text(text):
    if not EMBED_CMD:
        return None
    try:
        p = subprocess.run(["/bin/sh", "-c", EMBED_CMD], input=text, text=True,
                           capture_output=True, timeout=EMBED_TIMEOUT)
        if p.returncode != 0:
            return None
        vec = json.loads(p.stdout)
        if isinstance(vec, dict):
            vec = vec.get("embedding") or vec.get("data") or vec.get("vector")
        if not isinstance(vec, list) or not vec or not all(isinstance(x, (int, float)) for x in vec):
            return None
        return [float(x) for x in vec]
    except Exception:
        return None
# Decay/forgetting for volatile structured facts. Volatile facts (e.g.
# distilled task-progress) describe ephemeral working state; once older than
# this TTL they are dropped from the index so they stop polluting recall weeks
# later. Durable facts (preferences/decisions) never decay. Only memory-facts
# entries are affected — MEMORY.md/USER.md/wiki/honcho docs are always indexed.
# Set CCC_MEMORY_VOLATILE_TTL_DAYS=0 to disable decay entirely.
try:
    VOLATILE_TTL_DAYS = float(os.environ.get("CCC_MEMORY_VOLATILE_TTL_DAYS", "14") or 14)
except ValueError:
    VOLATILE_TTL_DAYS = 14.0


def fact_age_days(observed_at):
    """Age of a fact in days from its observed_at, or None if unparseable.

    Fail-safe: missing/garbage timestamps return None so the caller keeps the
    fact (we never forget a fact just because we couldn't read its date).
    """
    s = (observed_at or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    dt = None
    for candidate in (s, s[:19]):
        try:
            dt = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


db = Path(db_path)
state = Path(state_dir)
mem = Path(memory_dir)
cache = Path(cache_dir)
facts_file = Path(facts_file_arg)
index_distill_enabled = index_distill.lower() in {"1", "true", "yes", "on"}
disable_fts5 = os.environ.get("CCC_MEMORY_DISABLE_FTS5", "").lower() in {"1", "true", "yes", "on"}
fts5_enabled = False

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

    for row in structured_fact_docs(facts_file):
        candidates.append(row)

    # Distill artifacts can include raw transcript fragments. Keep them opt-in.
    if index_distill_enabled:
        for name in ("distill-last.json", "wiki-candidates.md"):
            candidates.append(("state", state / name))
        hist = state / "distill-history"
        if hist.is_dir():
            for p in sorted(hist.glob("*.json"))[-200:]:
                candidates.append(("distill-history", p))

    for item in candidates:
        if len(item) == 3:
            kind, path, text = item
            if text:
                yield kind, str(path), text
            continue
        kind, p = item
        if not p.is_file():
            continue
        text = read_json_text(p) if p.suffix == ".json" else redact_text(read_text(p))
        if text:
            yield kind, str(p), text




def normalize_for_hash(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def structured_fact_docs(path: Path):
    """Yield reviewed/local structured memory facts as indexable local docs.

    JSONL is optional and local-only. It is intended for distilled facts, not raw
    transcript blobs. Rejected facts are skipped, duplicate normalized text is
    skipped, and all text passes through the same redaction boundary as other
    memory sources.
    """
    if not path.is_file():
        return
    seen_text = set()
    for line_no, raw in enumerate(read_text(path).splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            text = redact_text(raw)
            obj = {"id": f"line-{line_no}", "kind": "unstructured", "text": text, "review": "needs-human"}
        if not isinstance(obj, dict):
            continue
        review = str(obj.get("review") or "auto-local").lower()
        # 'superseded' = a near-duplicate consolidated away by ccc-memory-consolidate;
        # kept in the file as an audit trail but not injected.
        if review in ("rejected", "superseded"):
            continue
        kind = str(obj.get("kind") or "fact")
        durability = str(obj.get("durability") or ("volatile" if kind == "task-progress" else "durable")).lower()
        observed_at = str(obj.get("observed_at") or "")
        # Decay: drop volatile facts past the TTL before they consume a dedup
        # slot or get indexed. Durable facts and undated facts are kept.
        if durability == "volatile" and VOLATILE_TTL_DAYS > 0:
            age = fact_age_days(observed_at)
            if age is not None and age > VOLATILE_TTL_DAYS:
                continue
        text = redact_text(str(obj.get("text") or obj.get("summary") or ""))
        if not text:
            continue
        norm = normalize_for_hash(text)
        if norm in seen_text:
            continue
        seen_text.add(norm)
        privacy = str(obj.get("privacy") or "private")
        confidence = obj.get("confidence", "")
        valid_from = str(obj.get("valid_from") or "")
        valid_until = str(obj.get("valid_until") or "")
        def listish(name):
            v = obj.get(name) or []
            if isinstance(v, list):
                return ", ".join(redact_text(str(x)) for x in v[:20])
            return redact_text(str(v))
        source = obj.get("source") if isinstance(obj.get("source"), dict) else {}
        source_text = " ".join(str(source.get(k) or "") for k in ("type", "path", "span"))
        content = redact_text("\n".join([
            f"kind: {kind}",
            f"durability: {durability}",
            f"privacy: {privacy}",
            f"review: {review}",
            f"confidence: {confidence}",
            f"observed_at: {observed_at}",
            f"valid_from: {valid_from}",
            f"valid_until: {valid_until}",
            f"entities: {listish('entities')}",
            f"tags: {listish('tags')}",
            f"source: {source_text}",
            f"text: {text}",
        ]))
        fid = str(obj.get("id") or f"line-{line_no}")
        yield "structured", f"{path}#L{line_no}:{fid}", content


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
    if disable_fts5:
        con.execute("DROP TABLE IF EXISTS memory_fts")
    else:
        try:
            con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(path UNINDEXED, source UNINDEXED, content)")
            fts5_enabled = True
        except sqlite3.OperationalError:
            fts5_enabled = False
    if cmd == "rebuild":
        con.execute("DELETE FROM memory_docs")
        if fts5_enabled:
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
        if fts5_enabled:
            con.execute("DELETE FROM memory_fts")
            con.execute("INSERT INTO memory_fts(path,source,content) SELECT path,source,content FROM memory_docs")
        con.commit()
        # Optional semantic-lane embeddings, computed incrementally over the
        # redacted memory_docs content. Skipped entirely when CCC_MEMORY_EMBED_CMD
        # is unset; fail-open per doc (a failed embed leaves the doc without a
        # vector, so the semantic lane just has fewer candidates).
        if EMBED_CMD:
            con.execute("CREATE TABLE IF NOT EXISTS memory_vectors (path TEXT PRIMARY KEY, content_hash TEXT, model TEXT, dim INTEGER, vec TEXT)")
            existing = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, content_hash, model FROM memory_vectors")}
            keep = set()
            for path, content in con.execute("SELECT path, content FROM memory_docs").fetchall():
                keep.add(path)
                h = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
                if existing.get(path) == (h, EMBED_MODEL):
                    continue  # unchanged under the same model -> no re-embed (cost control)
                vec = embed_text(content or "")
                if vec is None:
                    continue
                con.execute(
                    "INSERT INTO memory_vectors(path,content_hash,model,dim,vec) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET content_hash=excluded.content_hash, model=excluded.model, dim=excluded.dim, vec=excluded.vec",
                    (path, h, EMBED_MODEL, len(vec), json.dumps(vec)),
                )
            con.executemany("DELETE FROM memory_vectors WHERE path = ?", [(p,) for p in existing if p not in keep])
            con.commit()
        # Compact after update/rebuild so replaced/deleted plaintext from older
        # index versions is not left in SQLite free pages.
        con.execute("VACUUM")
    count = con.execute("SELECT COUNT(*) FROM memory_docs").fetchone()[0]
finally:
    con.close()
    secure_db_files(db)

print(json.dumps({"ok": True, "db": str(db), "documents": count, "distill_indexed": index_distill_enabled, "fts5_enabled": fts5_enabled}, ensure_ascii=False))
