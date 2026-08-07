#!/usr/bin/env python3
"""nunchi — thin peer/dialectic layer over local SQLite (Wiki TM-1330/TM-1332, #816).

Shadow of Honcho's working/relational memory. Ingests the SAME distill
extraction ({honcho:[{kind,text,subject}], ...}) that honcho-push.sh sends to
Honcho — zero extra LLM cost. Recall is local FTS5; dialectic adds one
query-time Haiku synthesis call and, when a MemPalace palace is present,
combines verbatim transcript excerpts (decision §3). Falls back to
peer_facts-only when MemPalace is absent.

Canonicalized from the 3-node hand-deployed pilot with the TM-1332 backlog:
  B1  facts_fts indexes (fact, observed) — node-name queries work; existing
      pilot DBs are migrated in place on first `init`/open.
  B2  kind=correction auto-supersedes the best-matching open fact for the
      same observed peer (conservative: token-overlap >= 0.5, single fact).
  B3  node persona aliases (카렐렌→yukson, 등애→dungae, …) normalized at
      ingest and expanded at query time.

Write gate (#890 — graph-engineering review):
  G1  progress→done update detection auto-closes the stale in-flight fact
      (same observed, token overlap, old has a progress marker, new has a
      completion marker). Reversible: clear valid_to; `supersedes` keeps the
      link. Measured before G1: 75 facts, ~30% stale, 0 supersedes used.
  G2  source_rank (3 user-stated / 2 measured / 1 inferred), verified against
      the transcript quote — never self-reported alone. A claimed rank without
      a verifiable quote is demoted to 1 and flagged for review. A lower-rank
      fact never closes a higher-rank fact (an agent inference must not bury
      a user statement).
  G3  ambiguous high-overlap conflicts are NEVER auto-resolved — both stay
      open, the newcomer is flagged review=1 and surfaced in the snapshot.
  G4  kind=constraint (금지/필수 rules) is stored near-verbatim and always
      injected by `snapshot` regardless of the recency limit.

Usage:
  nunchi.py init
  nunchi.py ingest <distill.json | ->     # mirror distill honcho[] items
  nunchi.py recall <query> [--target T] [--limit N]
  nunchi.py dialectic <query> [--target T]   # facts + MemPalace + Haiku
  nunchi.py supersede <fact_id> <new text>   # close old, insert new
  nunchi.py snapshot [--limit N]             # SessionStart-ready summary
  nunchi.py review-stale [--close]           # G1 retro pass over old facts
  nunchi.py metrics                          # body-free counters for bench
  nunchi.py stats
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

DB = os.environ.get("NUNCHI_DB", os.path.expanduser("~/.nunchi/facts.db"))
SNAPSHOT = os.environ.get("NUNCHI_SNAPSHOT", os.path.expanduser("~/.nunchi/snapshot.md"))
USER_PEER = "seo-jin-on"
OBSERVER = "family-assistant"  # TM-1249 unified distill author peer

# B3 — node persona/hangul aliases → canonical node slug (DOC-140 12 nodes).
# Personas (e.g. yukson's "카렐렌") drift into fact text and subjects; queries
# and observed values must converge on the canonical slug or node-name recall
# silently misses (measured on yukson: 57 node facts unreachable).
NODE_ALIASES = {
    "dungae": "dungae", "등애": "dungae",
    "soonwook": "soonwook", "순욱": "soonwook",
    "yukson": "yukson", "육손": "yukson", "카렐렌": "yukson", "karellen": "yukson",
    "gongmyoung": "gongmyoung", "공명": "gongmyoung",
    "gongyung": "gongyung", "공융": "gongyung",
    "seoseo": "seoseo", "서서": "seoseo",
    "sogyo": "sogyo", "소교": "sogyo",
    "nosuk": "nosuk", "노숙": "nosuk",
    "bangtong": "bangtong", "방통": "bangtong",
    "jingun": "jingun", "진군": "jingun",
    "daegyo": "daegyo", "대교": "daegyo",
    "gwakga": "gwakga", "곽가": "gwakga",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS peer_facts (
  id INTEGER PRIMARY KEY,
  observer TEXT NOT NULL,
  observed TEXT NOT NULL,
  kind TEXT NOT NULL,
  fact TEXT NOT NULL,
  evidence TEXT,
  confidence REAL DEFAULT 0.7,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  supersedes INTEGER REFERENCES peer_facts(id),
  dedup TEXT UNIQUE,
  created_at TEXT NOT NULL,
  source_rank INTEGER DEFAULT 1,
  review INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_peer ON peer_facts(observer, observed, valid_to);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
  fact, observed, content='peer_facts', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON peer_facts BEGIN
  INSERT INTO facts_fts(rowid, fact, observed) VALUES (new.id, new.fact, new.observed); END;
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate_fts(c):
    """B1 — upgrade a pilot-era single-column facts_fts to (fact, observed).

    Cheap (pilot DBs are a few hundred rows) and safe: on any failure we keep
    the old table so recall degrades to fact-text-only instead of breaking.
    """
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(facts_fts)")]
        if "observed" in cols:
            return
        c.executescript(
            "DROP TRIGGER IF EXISTS facts_ai; DROP TABLE IF EXISTS facts_fts;")
        c.executescript(SCHEMA)
        c.execute("INSERT INTO facts_fts(rowid, fact, observed)"
                  " SELECT id, fact, observed FROM peer_facts")
        c.commit()
    except sqlite3.Error:
        pass


def _migrate_gate_cols(c):
    """#890 — add write-gate columns to a pre-gate DB, lossless in place."""
    cols = [r[1] for r in c.execute("PRAGMA table_info(peer_facts)")]
    for name, ddl in (("source_rank", "source_rank INTEGER DEFAULT 1"),
                      ("review", "review INTEGER DEFAULT 0")):
        if name not in cols:
            try:
                c.execute(f"ALTER TABLE peer_facts ADD COLUMN {ddl}")
                c.commit()
            except sqlite3.Error:
                pass


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    _migrate_fts(c)
    _migrate_gate_cols(c)
    return c


def _node_name():
    if os.environ.get("CCC_NODE"):
        return os.environ["CCC_NODE"]
    try:
        return open(os.path.expanduser("~/.claude/state/node.txt")).read().strip()
    except OSError:
        return "unknown"


def canon_node(name):
    """Canonical node slug for a name/persona/hangul alias, else None."""
    if not name:
        return None
    key = str(name).strip()
    return NODE_ALIASES.get(key) or NODE_ALIASES.get(key.lower())


def map_observed(subject, session_id):
    if subject == "user":
        return USER_PEER
    if subject == "node":
        return canon_node(_node_name()) or _node_name()
    # B3: a distiller that emits a node name/persona as subject still lands
    # on the canonical slug instead of forking a new observed peer.
    aliased = canon_node(subject)
    if aliased:
        return aliased
    return f"session:{session_id}"


def _tokens(text):
    return {w for w in str(text).split() if len(w) > 1}


# Mutable operational-state patterns (#1010). Commit SHAs, systemd status
# counters/phrases, and "back to normal" claims are live-check targets
# (CLAUDE.md), not durable memory: they age into wrong values and re-fire
# the G3 review gate on every re-extraction, polluting the review queue.
# Applied to kind=fact only — corrections/constraints carry durable intent
# and are never filtered here. The SHA-ish token requires at least one a-f
# letter so bare date stamps (e.g. 20260807) are not misclassified.
_MUTABLE_OPS = re.compile(
    r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"   # commit SHA / digest fragment
    r"|\bNRestarts=\d+"                        # systemd restart counter
    r"|\bMain\s?PID=\d+"                       # systemd main pid
    r"|\bactive\s*\((?:running|exited|failed|waiting|deactivating)\)"
    r"|\b(?:active|enabled|disabled|inactive)\s*/\s*(?:active|enabled|disabled|inactive)\b"
    r"|정상\s*(?:상태|화|으로\s*(?:기록|동작|복구|작동))",
    re.IGNORECASE)


def _is_mutable_ops_fact(text):
    return bool(_MUTABLE_OPS.search(text))


def _auto_supersede(c, observed, correction_text, sid):
    """B2 — close the open fact this correction most plausibly replaces.

    Conservative on purpose: same observed peer only, token-overlap of the
    correction against the candidate must be >= 0.5, and at most one fact is
    closed. Returns the superseded fact id or None. Reversible: valid_to can
    be cleared by hand and the link is kept in `supersedes`.
    """
    corr = _tokens(correction_text)
    if not corr:
        return None
    best, best_ratio = None, 0.0
    rows = c.execute(
        "SELECT id, fact FROM peer_facts WHERE observed=? AND valid_to IS NULL",
        (observed,)).fetchall()
    for fid, fact in rows:
        inter = len(corr & _tokens(fact))
        ratio = inter / len(corr)
        if ratio > best_ratio:
            best, best_ratio = fid, ratio
    if best is not None and best_ratio >= 0.5:
        c.execute("UPDATE peer_facts SET valid_to=? WHERE id=?", (now(), best))
        return best
    return None


# ---- #890 write gate --------------------------------------------------------
# G1 markers. Deliberately short lists: a marker miss only means the stale
# fact waits for the retro pass / review queue instead of auto-closing.
_PROGRESS = re.compile(
    r"진행\s?중|진행중|대기\s?중|대기|실행\s?중|예정|보류|승인\s대기|미설치|미완료|착수|작업\s?중")
_DONE = re.compile(
    r"완료|완결|종결|머지|MERGED|병합|해소|확정|성공|폐기|설치됨|닫힘|CLOSED|배포됨", re.IGNORECASE)

_SOURCE_RANK = {"user-stated": 3, "measured": 2, "inferred": 1}


def _quote_key(text):
    """Skeleton for quote matching: drop whitespace and backslashes so a
    verbatim quote still matches its JSON-escaped form (\\n, \\") inside a
    transcript .jsonl."""
    return re.sub(r"[\s\\]+", "", str(text))


def _verify_rank(item, transcript_key):
    """G2 — a self-reported source label counts only with a verifiable quote.

    Returns (rank, review). user-stated/measured claims need their quote to
    actually appear in the transcript (skeleton substring); otherwise the
    fact is demoted to inferred(1) and flagged for review. Legacy payloads
    without a source field stay rank 1, no review noise.
    """
    source = (item.get("source") or "").strip()
    if source not in _SOURCE_RANK:
        return 1, 0
    rank = _SOURCE_RANK[source]
    if rank == 1:
        return 1, 0
    quote = _quote_key(item.get("quote") or "")
    if len(quote) >= 8 and transcript_key and quote in transcript_key:
        return rank, 0
    return 1, 1


def _g1_pool(c, observed):
    """G1 candidate pool. session:* peers are one actor's in-flight log split
    by session id — a completion routinely lands in a later session than the
    fact it closes (measured retro on dungae: same-observed matching found 1
    of ~20 stale facts). Cross-match all session peers; user/node peers stay
    strictly scoped."""
    if str(observed).startswith("session:"):
        return c.execute(
            "SELECT id, fact, source_rank FROM peer_facts"
            " WHERE (observed=? OR observed LIKE 'session:%') AND valid_to IS NULL",
            (observed,)).fetchall()
    return c.execute(
        "SELECT id, fact, source_rank FROM peer_facts"
        " WHERE observed=? AND valid_to IS NULL", (observed,)).fetchall()


def _update_supersede(c, observed, text, new_rank):
    """G1 — close the in-flight fact this completion most plausibly updates.

    Old fact carries a progress marker, the new text a completion marker,
    token overlap >= 0.4, and the newcomer's source rank is not lower than
    the old fact's (G2 guard). One fact at most, reversible exactly like B2
    (clear valid_to; `supersedes` keeps the link).
    """
    if not _DONE.search(text):
        return None
    new = _tokens(text)
    if not new:
        return None
    best, best_ratio = None, 0.0
    rows = _g1_pool(c, observed)
    for fid, fact, old_rank in rows:
        if not _PROGRESS.search(fact) or (old_rank or 1) > new_rank:
            continue
        old = _tokens(fact)
        if not old:
            continue
        ratio = len(new & old) / min(len(new), len(old))
        if ratio > best_ratio:
            best, best_ratio = fid, ratio
    if best is not None and best_ratio >= 0.4:
        c.execute("UPDATE peer_facts SET valid_to=? WHERE id=?", (now(), best))
        return best
    return None


def _conflict_review(c, observed, text):
    """G3 — flag (never resolve) a suspicious high-overlap open sibling.

    A newcomer overlapping an open same-peer fact >= 0.6 without matching the
    G1 update pattern is a potential contradiction or drifted duplicate: both
    stay open and the newcomer is flagged review=1 for the owner queue.
    """
    new = _tokens(text)
    if not new:
        return 0
    for _fid, fact, _r in c.execute(
            "SELECT id, fact, source_rank FROM peer_facts"
            " WHERE observed=? AND valid_to IS NULL", (observed,)).fetchall():
        old = _tokens(fact)
        if old and len(new & old) / min(len(new), len(old)) >= 0.6:
            return 1
    return 0


def _transcript_text(payload):
    """Skeleton transcript body for G2 quote checks, '' when unavailable.

    History payloads carry no transcript_path, so fall back to deriving the
    SDK transcript location from (source_cwd, session_id) the same way the
    CLI encodes project dirs (path separators and dots become dashes).
    """
    path = payload.get("transcript_path") or ""
    if not path:
        sid = str(payload.get("session_id") or "")
        cwd = str(payload.get("source_cwd") or "")
        if re.fullmatch(r"[0-9a-f-]{8,64}", sid) and cwd.startswith("/"):
            enc = re.sub(r"[/.]", "-", cwd)
            path = os.path.expanduser(f"~/.claude/projects/{enc}/{sid}.jsonl")
    try:
        if path and os.path.exists(path) and os.path.getsize(path) < 64 * 1024 * 1024:
            return _quote_key(open(path, encoding="utf-8", errors="replace").read())
    except OSError:
        pass
    return ""


def ingest(path):
    payload = json.load(sys.stdin if path == "-" else open(path))
    sid = payload.get("session_id", "unknown")
    items = payload.get("honcho", [])
    auto_supersede = os.environ.get("NUNCHI_NO_AUTO_SUPERSEDE") != "1"
    transcript = _transcript_text(payload)
    c = db()
    n = 0
    skipped_mutable = 0
    for it in items:
        # Gate order matters (#890): normalize → rank-verify → update/close →
        # conflict-review → dedup. Normalizing first is what lets the same
        # fact spelled through an alias land on one observed peer before the
        # duplicate check runs.
        text = (it.get("text") or "").strip()
        if not text:
            continue
        observed = map_observed(it.get("subject"), sid)
        kind = it.get("kind", "fact")
        # #1010: transient operational-state facts never enter the store.
        if kind == "fact" and _is_mutable_ops_fact(text):
            skipped_mutable += 1
            continue
        rank, review = _verify_rank(it, transcript)
        superseded = None
        if auto_supersede:
            if kind == "correction":
                superseded = _auto_supersede(c, observed, text, sid)
            else:
                superseded = _update_supersede(c, observed, text, rank)
        if superseded is None and kind not in ("correction", "constraint"):
            review = review or _conflict_review(c, observed, text)
        dedup = hashlib.sha1(f"{OBSERVER}|{it.get('subject')}|{text}".encode()).hexdigest()
        evidence = (f"auto:correction:{sid}" if kind == "correction" and superseded is not None
                    else f"auto:update:{sid}" if superseded is not None
                    else f"distill:{sid}")
        try:
            c.execute(
                "INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,"
                "supersedes,dedup,created_at,source_rank,review)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (OBSERVER, observed, kind, text, evidence,
                 payload.get("distilled_at") or now(), superseded, dedup, now(),
                 rank, review))
            n += 1
        except sqlite3.IntegrityError:
            pass  # duplicate fact already stored
    c.commit()
    print(f"ingested {n}/{len(items)} facts (session={sid}, skipped_mutable_ops={skipped_mutable})")


def _expand_query(query):
    """B3 — widen the FTS query with canonical slugs for any alias tokens."""
    words = [w for w in str(query).split() if len(w) > 1]
    extra = []
    for w in words:
        slug = canon_node(w)
        if slug and slug not in words and slug not in extra:
            extra.append(slug)
    return words + extra


def search(c, query, target=None, limit=10, include_history=False):
    where = "f.valid_to IS NULL" if not include_history else "1=1"
    args = []
    if target:
        where += " AND f.observed = ?"
        args.append(target)
    words = _expand_query(query)
    q = " OR ".join(f'"{w}"' for w in words) or f'"{query}"'
    try:
        rows = c.execute(
            f"SELECT f.id,f.observed,f.kind,f.fact,f.valid_from,f.valid_to,bm25(facts_fts) r"
            f" FROM facts_fts x JOIN peer_facts f ON f.id=x.rowid"
            f" WHERE facts_fts MATCH ? AND {where}"
            f" ORDER BY r LIMIT ?", [q] + args + [limit]).fetchall()
    except sqlite3.OperationalError:
        like = f"%{query}%"
        rows = c.execute(
            f"SELECT id,observed,kind,fact,valid_from,valid_to,0 FROM peer_facts f"
            f" WHERE (fact LIKE ? OR observed LIKE ?) AND {where} ORDER BY id DESC LIMIT ?",
            [like, like] + args + [limit]).fetchall()
    return rows


def recall(query, target, limit):
    for r in search(db(), query, target, limit, include_history=True):
        closed = f" ~{r[5]}]" if r[5] else ""
        print(f"[#{r[0]} {r[1]} ({r[2]}) {r[4][:10]}{closed} {r[3]}")


def llm_synthesize(prompt):
    """Query-time synthesis backend: claude -p haiku if present, else codex exec."""
    import shutil
    env = {**os.environ, "CLAUDE_DISTILL_INFLIGHT": "1"}
    if shutil.which("claude"):
        cmd = ["claude", "-p", prompt, "--model", "haiku"]
    elif shutil.which("codex"):
        cmd = ["codex", "exec", "--skip-git-repo-check", prompt]
    else:
        return "(합성 백엔드 없음: claude/codex CLI 부재)"
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
    text = out.stdout.strip()
    if shutil.which("codex") and not shutil.which("claude"):
        # codex exec prints session log lines; the answer is the final block
        lines = [ln for ln in text.splitlines() if ln.strip()]
        text = lines[-1] if lines else ""
    return text or out.stderr.strip()


_KW_STOP = {
    "the", "a", "an", "of", "to", "in", "and", "or", "is", "are", "was", "were",
    "what", "which", "how", "when", "where", "who", "why", "did", "do", "does",
    "무엇", "어떻게", "어디", "어느", "언제", "누가", "왜", "그리고", "또는",
    "있다", "없다", "한다", "했다", "된다", "됐다", "기억나", "알려줘", "말해줘",
}
_KW_PARTICLE = re.compile(
    r"(에서는|에서|으로|이라|이란|인가|인지|한다|했다|합니까|입니까"
    r"|은|는|이|가|을|를|의|에|로|와|과|도|만)$")


def _keywords(query, max_terms=6):
    """Project a natural-language query onto its content words (#824 Phase 1).

    Strips common Korean particles/aux endings and stopwords so an exact-word
    MemPalace query can be formed from a paraphrased question.
    """
    out = []
    for tok in re.findall(r"[A-Za-z0-9_.\-/]+|[가-힣]+", query):
        base = _KW_PARTICLE.sub("", tok) if re.fullmatch(r"[가-힣]+", tok) else tok
        if len(base) >= 2 and base.lower() not in _KW_STOP and base not in _KW_STOP \
           and base not in out:
            out.append(base)
    return out[:max_terms]


def _mp_search(mp, query, n):
    """One MemPalace CLI search, cleaned to excerpt lines. [] on any failure."""
    try:
        child_env = os.environ.copy()
        palace_home = os.environ.get("CCC_NUNCHI_MEMPALACE_HOME")
        if palace_home:
            child_env["HOME"] = palace_home
        out = subprocess.run([mp, "search", query, "--results", str(n)],
                             capture_output=True, text=True, timeout=30,
                             env=child_env)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    keep = []
    for ln in out.stdout.splitlines():
        s = ln.strip()
        if not s or s.startswith("=") or s.startswith("─") or s.startswith("Results for") \
           or s.startswith("Source:") or s.startswith("Match:") or s.startswith("MemPalace"):
            continue
        keep.append(s)
    return keep


def mempalace_verbatim(query, n=3):
    """Decision §3 verbatim layer — pull transcript excerpts via MemPalace CLI.

    Hybrid retrieval (#824 Phase 1): the embedding search is paraphrase-
    sensitive (TM-1332 Q4 — a natural-language query missed excerpts an
    exact-word query recovered), so we search twice — the raw query plus its
    keyword projection — and merge order-preserving, de-duplicated.

    Silent empty string on any failure (no CLI, no palace, timeout) so
    dialectic degrades to peer_facts-only. MemPalace is an external uv tool,
    never vendored here (#816).
    """
    import shutil
    mp = shutil.which("mempalace") or os.path.expanduser("~/.local/bin/mempalace")
    if not os.path.exists(mp):
        return ""
    lines = _mp_search(mp, query, n)
    kw = " ".join(_keywords(query))
    if kw and kw.strip().lower() != query.strip().lower():
        for s in _mp_search(mp, kw, n):
            if s not in lines:
                lines.append(s)
    return "\n".join(lines[:40])


def dialectic(query, target):
    c = db()
    facts = search(c, query, target, limit=12, include_history=True)
    current = [f"- {r[3]}" for r in facts if not r[5]]
    history = [f"- ({r[4][:10]}~{r[5][:10]}) {r[3]}" for r in facts if r[5]]
    verbatim = mempalace_verbatim(query, 3)   # §3: verbatim layer, optional
    prompt = (
        "아래는 peer 메모리(요약 사실)와 MemPalace(원문 발췌)에서 검색한 근거다. 질문에 한국어로 간결·정확히 답하라.\n"
        "근거에 없는 내용은 지어내지 말고 '기록 없음'이라 하라. 시일이 닫힌(valid_to 있는) 사실은 과거 이력이다.\n\n"
        f"질문: {query}\n\n[peer 사실 — 현재 유효]\n" + ("\n".join(current) or "(없음)") +
        "\n\n[peer 사실 — 과거 이력]\n" + ("\n".join(history) or "(없음)") +
        "\n\n[MemPalace 원문 발췌]\n" + (verbatim or "(없음)"))
    print(llm_synthesize(prompt))


def supersede(fid, new_text):
    c = db()
    row = c.execute("SELECT observer,observed,kind FROM peer_facts WHERE id=?", (fid,)).fetchone()
    if not row:
        sys.exit(f"no fact #{fid}")
    c.execute("UPDATE peer_facts SET valid_to=? WHERE id=?", (now(), fid))
    dedup = hashlib.sha1(f"{row[0]}|manual|{new_text}".encode()).hexdigest()
    c.execute(
        "INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,supersedes,dedup,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (row[0], row[1], row[2], new_text, "manual:supersede", now(), fid, dedup, now()))
    c.commit()
    print(f"#{fid} closed; inserted successor")


def snapshot(limit):
    c = db()
    rows = c.execute(
        "SELECT observed,kind,fact FROM peer_facts WHERE valid_to IS NULL"
        " AND kind != 'constraint' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    # G4 — constraints are never crowded out by recency: a rule you must not
    # break is exactly the fact whose miss is an incident, not a staleness.
    cons = c.execute(
        "SELECT observed,fact FROM peer_facts WHERE valid_to IS NULL"
        " AND kind = 'constraint' ORDER BY id DESC").fetchall()
    pending = c.execute(
        "SELECT COUNT(*) FROM peer_facts WHERE valid_to IS NULL AND review=1"
    ).fetchone()[0]
    lines = ["## nunchi working memory (primary — gate-3 transition, #824)"]
    if pending:
        lines.append(
            f"- ⚠ 검토대기 {pending}건 (충돌/미검증 출처) — `nunchi.py review`로 확인")
    for o, f in cons:
        lines.append(f"- [제약/{o}] {f}")
    lines += [f"- ({o}/{k}) {f}" for o, k, f in rows]
    text = "\n".join(lines)
    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    open(SNAPSHOT, "w").write(text)
    print(text)


def review(fact_id=None, clear=False):
    """Owner queue for G2/G3 flags — list open review facts, or clear one.

    A queue nobody can read is how audit data rots (the lineage lesson):
    the snapshot points here, and clearing is explicit and per-fact.
    """
    c = db()
    if fact_id is not None:
        if clear:
            c.execute("UPDATE peer_facts SET review=0 WHERE id=?", (fact_id,))
            c.commit()
            print(f"#{fact_id} review flag cleared")
        return
    rows = c.execute(
        "SELECT id,observed,kind,fact,source_rank FROM peer_facts"
        " WHERE valid_to IS NULL AND review=1 ORDER BY id").fetchall()
    for fid, o, k, f, r in rows:
        print(f"#{fid} rank={r} {o} ({k}) {f}")
    print(f"{len(rows)} pending — clear: nunchi.py review <id> --clear;"
          " replace: nunchi.py supersede <id> <new text>")


def review_stale(do_close):
    """G1 applied retroactively (#890 P3) — list, then close on approval.

    Pairs each open progress-marker fact with a NEWER open same-peer fact
    carrying a completion marker and >= 0.4 overlap. Print-only by default;
    --close (after owner approval) sets valid_to. supersedes on the newer
    fact is left untouched — retro-linking would rewrite ingest history.
    """
    c = db()
    rows = c.execute(
        "SELECT id, observed, fact, source_rank FROM peer_facts"
        " WHERE valid_to IS NULL ORDER BY id").fetchall()
    # Mirror _g1_pool: all session:* peers form one retro pool; user/node
    # peers stay scoped to themselves.
    by_obs = {}
    for r in rows:
        key = "session:*" if r[1].startswith("session:") else r[1]
        by_obs.setdefault(key, []).append(r)
    cands = []
    for facts in by_obs.values():
        for i, (fid, _o, fact, rank) in enumerate(facts):
            if not _PROGRESS.search(fact):
                continue
            old = _tokens(fact)
            for nfid, _o2, nfact, nrank in facts[i + 1:]:
                if not _DONE.search(nfact) or (nrank or 1) < (rank or 1):
                    continue
                newt = _tokens(nfact)
                if old and newt and len(old & newt) / min(len(old), len(newt)) >= 0.4:
                    cands.append((fid, nfid, fact))
                    break
    for fid, nfid, fact in cands:
        print(f"#{fid} stale (superseded by #{nfid}): {fact[:90]}")
    if do_close:
        for fid, _nfid, _f in cands:
            c.execute("UPDATE peer_facts SET valid_to=? WHERE id=?", (now(), fid))
        c.commit()
        print(f"closed {len(cands)} facts (reversible: clear valid_to)")
    else:
        print(f"{len(cands)} candidates — apply with: nunchi.py review-stale --close")


def metrics():
    """Body-free counters for the weekly bench (#890 P5) — key=value lines."""
    from datetime import timedelta
    c = db()
    total, open_ = c.execute(
        "SELECT COUNT(*), COALESCE(SUM(valid_to IS NULL),0) FROM peer_facts").fetchone()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    stale_suspect = sum(
        1 for (fact, vfrom) in c.execute(
            "SELECT fact, valid_from FROM peer_facts WHERE valid_to IS NULL")
        if _PROGRESS.search(fact) and vfrom < cutoff)
    pending = c.execute(
        "SELECT COUNT(*) FROM peer_facts WHERE valid_to IS NULL AND review=1").fetchone()[0]
    cons = c.execute(
        "SELECT COUNT(*) FROM peer_facts WHERE valid_to IS NULL AND kind='constraint'"
    ).fetchone()[0]
    closed = total - open_
    ratio = (stale_suspect / open_) if open_ else 0.0
    print(f"facts_total={total}")
    print(f"facts_open={open_}")
    print(f"facts_closed={closed}")
    print(f"stale_suspect_open={stale_suspect}")
    print(f"stale_suspect_ratio={ratio:.2f}")
    print(f"review_pending={pending}")
    print(f"constraints_open={cons}")


def stats():
    c = db()
    total, open_ = c.execute(
        "SELECT COUNT(*), SUM(valid_to IS NULL) FROM peer_facts").fetchone()
    by = c.execute(
        "SELECT observed, COUNT(*) FROM peer_facts WHERE valid_to IS NULL"
        " GROUP BY observed ORDER BY 2 DESC LIMIT 10").fetchall()
    print(f"facts: {total} total, {open_} current")
    for o, n in by:
        print(f"  {o}: {n}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    args = sys.argv[2:]
    def flag(name, default=None):
        return args[args.index(name) + 1] if name in args else default

    if cmd == "init":
        db()
        print(f"db ready: {DB}")
    elif cmd == "ingest":
        ingest(args[0])
    elif cmd == "recall":
        recall(args[0], flag("--target"), int(flag("--limit", 10)))
    elif cmd == "dialectic":
        dialectic(args[0], flag("--target"))
    elif cmd == "supersede":
        supersede(int(args[0]), args[1])
    elif cmd == "snapshot":
        snapshot(int(flag("--limit", 25)))
    elif cmd == "review":
        pos = [a for a in args if not a.startswith("--")]
        review(int(pos[0]) if pos else None, clear="--clear" in args)
    elif cmd == "review-stale":
        review_stale("--close" in args)
    elif cmd == "metrics":
        metrics()
    elif cmd == "stats":
        stats()
    else:
        sys.exit(__doc__)
