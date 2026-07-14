#!/usr/bin/env python3
# ruff: noqa: E401,E402,E701,E702
"""Query the local hot-memory SQLite index."""

import os
import sys
from pathlib import Path

STATE_DIR = os.environ.get("CCC_STATE_DIR") or f"{os.environ.get('HOME', '/root')}/.claude/state"
DB = os.environ.get("CCC_MEMORY_INDEX_DB") or f"{STATE_DIR}/memory-index.sqlite"
QUERY = sys.argv[1] if len(sys.argv) > 1 else ""
LIMIT = os.environ.get("CCC_MEMORY_SEARCH_LIMIT") or "5"
RETRIEVAL = os.environ.get("CCC_MEMORY_RETRIEVAL") or "fts"

if not QUERY:
    print(f"usage: {Path(sys.argv[0]).name} <query>", file=sys.stderr)
    raise SystemExit(2)
if not Path(DB).is_file():
    print(f"memory index missing: {DB}", file=sys.stderr)
    raise SystemExit(1)

sys.argv = [sys.argv[0], DB, QUERY, LIMIT, RETRIEVAL]

import hashlib, json, math, os, re, sqlite3, subprocess, sys, tempfile, time
from datetime import datetime, timezone
path, query, retrieval = sys.argv[1], sys.argv[2], sys.argv[4]
# Guard the limit parse like every other numeric env var here: a malformed
# CCC_MEMORY_SEARCH_LIMIT (e.g. "abc") must fall back, not crash with a
# traceback, and a non-positive value (0/-1 → empty results) falls back too.
try:
    limit = int(sys.argv[3])
except (TypeError, ValueError):
    limit = 5
if limit <= 0:
    limit = 5
con=sqlite3.connect(path)
con.row_factory=sqlite3.Row
wiki_enabled = os.environ.get("CCC_WIKI_MEMORY_ENABLED", "1").lower() not in {"0", "false", "off", "no"}
if os.environ.get("CCC_NODE_ISOLATION_PROFILE", "fleet").lower() == "external":
    wiki_enabled = False

def hidden_by_wiki_boundary(row):
    if wiki_enabled:
        return False
    raw_path = str(row.get("path") or "")
    p = Path(raw_path)
    source = str(row.get("source") or "").lower()
    return (
        p.name in {"wiki.txt", "wiki-candidates.md", "distill-last.json"}
        or "distill-history" in p.parts
        or source.startswith("distill")
    )

# Optional semantic lane (operator-configured embedding provider; see the index
# tool). Doc vectors are precomputed in memory_vectors during refresh; here we
# embed the QUERY at search time with a tight timeout and fail-open — so this is
# the one lane that may touch the network, only when CCC_MEMORY_EMBED_CMD is set.
EMBED_CMD = os.environ.get("CCC_MEMORY_EMBED_CMD", "").strip()
try:
    EMBED_TIMEOUT = float(os.environ.get("CCC_MEMORY_EMBED_TIMEOUT", "15") or 15)
except ValueError:
    EMBED_TIMEOUT = 15.0
try:
    EMBED_MIN_SIM = float(os.environ.get("CCC_MEMORY_EMBED_MIN_SIM", "0.55") or 0.55)
except ValueError:
    EMBED_MIN_SIM = 0.55

# ---- usage feedback (retrieval-frequency weighting) ------------------------
# A closed, local feedback loop: docs that are repeatedly RETRIEVED for real
# injections earn a small, recency-decayed boost, so memory that keeps proving
# useful surfaces faster over time. Keyed by a content hash (stable across the
# path/line churn that local-facts append causes). Reading the boost is always
# on but a no-op until stats exist (no behavior change on a fresh node); WRITING
# happens only when the caller sets CCC_MEMORY_RECORD_USAGE=1 (load-memory's
# injection search), so ccc-memory-explain and tests stay read-only. The weight
# is small relative to token coverage (×4), so usage nudges ties — it does not
# let a popular-but-irrelevant doc outrank a strong lexical match.
# Disable the whole loop (read + write) with CCC_MEMORY_USAGE_FEEDBACK=0.
USAGE_ON = os.environ.get("CCC_MEMORY_USAGE_FEEDBACK", "1").strip().lower() not in {"0","false","off","no"}
USAGE_RECORD = USAGE_ON and os.environ.get("CCC_MEMORY_RECORD_USAGE", "").strip().lower() in {"1","true","on","yes"}
try:
    USAGE_WEIGHT = float(os.environ.get("CCC_MEMORY_USAGE_WEIGHT", "1.5") or 1.5)
except ValueError:
    USAGE_WEIGHT = 1.5
try:
    USAGE_TTL_DAYS = float(os.environ.get("CCC_MEMORY_USAGE_TTL_DAYS", "30") or 30)
except ValueError:
    USAGE_TTL_DAYS = 30.0
USAGE_MAX_ENTRIES = 2000
USAGE_PATH = os.environ.get("CCC_MEMORY_USAGE_FILE") or os.path.join(os.path.dirname(path) or ".", "memory-usage.json")

def _chash(content):
    # Charset matches char_ngrams() (Hiragana/Katakana/CJK included). The old
    # ASCII+Hangul-only set normalized predominantly Japanese/Chinese content to
    # "", so _chash returned "" and the usage-feedback loop (record/boost) could
    # never key such docs. Korean was already covered; this closes the JP/CN gap.
    norm = " ".join(re.findall(r"[0-9a-z가-힣぀-ヿ一-鿿]+", (content or "").lower()))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16] if norm else ""

def _load_usage():
    if not USAGE_ON:
        return {}
    try:
        with open(USAGE_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

USAGE = _load_usage()

def usage_signal(content):
    if not USAGE_ON or not USAGE:
        return 0.0
    rec = USAGE.get(_chash(content))
    if not isinstance(rec, dict):
        return 0.0
    n = rec.get("n") or 0
    if n <= 0:
        return 0.0
    age_days = max(0.0, (time.time() - (rec.get("t") or 0)) / 86400.0)
    decay = max(0.0, 1.0 - age_days / USAGE_TTL_DAYS) if USAGE_TTL_DAYS > 0 else 1.0
    # Cap below one token of coverage (token_hits×4) so usage only ever breaks
    # ties / nudges — a popular doc can never outrank a stronger lexical match.
    return round(min(USAGE_WEIGHT * math.log1p(n) * decay, 3.0), 4)

def record_usage(rows):
    # Best-effort, atomic, bounded, fail-open. Counts each surfaced doc once per
    # retrieval and stamps the time so the recency decay can fade stale popularity.
    if not USAGE_RECORD or not rows:
        return
    now = int(time.time())
    data = dict(USAGE)
    for r in rows:
        ch = r.get("_chash") or ""
        if not ch:
            continue
        rec = data.get(ch)
        if isinstance(rec, dict):
            rec["n"] = (rec.get("n") or 0) + 1
            rec["t"] = now
        else:
            data[ch] = {"n": 1, "t": now}
    if len(data) > USAGE_MAX_ENTRIES:  # evict least-recently-used
        data = dict(sorted(data.items(), key=lambda kv: (kv[1].get("t") or 0), reverse=True)[:USAGE_MAX_ENTRIES])
    try:
        d = os.path.dirname(USAGE_PATH) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".memory-usage.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, USAGE_PATH)
    except Exception:
        pass

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
    if not wiki_enabled:
        cands = [c for c in cands if not hidden_by_wiki_boundary(c)]
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
            "usage_boost": usage_signal(content),
        }
        if c.get("bm25") is not None:
            signals["fts_bm25"]=c["bm25"]
        score=(token_hits*4.0)+(phrase_hit*3.0)+signals["source_boost"]+signals["recency_boost"]+signals["durability_penalty"]+(tie*0.5)+signals["usage_boost"]
        out.append({"path":c["path"],"source":c["source"],"snippet":c["snippet"],
                    "score":round(score,4),"signals":signals,"_chash":_chash(content)})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out

def char_ngrams(s, n=3):
    # Character n-grams over a normalized stream (alnum + Hangul + CJK). This is
    # a stdlib, no-network fuzzy-recall signal: it matches morphological variants,
    # typos, and partial tokens that exact FTS tokenization misses — especially
    # for agglutinative Korean ("메모리" vs "메모리를"), where surface forms differ
    # but share character n-grams. It is NOT neural semantics (no synonymy across
    # scripts); a true embedding lane would slot into the same RRF fusion below.
    chars=re.findall(r"[0-9a-z가-힣぀-ヿ一-鿿]", s.lower())
    norm="".join(chars)
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i:i+n] for i in range(len(norm)-n+1)}

def fuzzy_scan(qgrams, n_over):
    # Full scan of the (small, bounded) hot index, scored by how much of the
    # query's char-ngram profile the doc covers (containment coefficient).
    if not qgrams:
        return []
    out=[]
    for r in con.execute("SELECT path, source, content, updated_at FROM memory_docs"):
        content=r["content"] or ""
        dg=char_ngrams(r["path"]+" "+content)
        if not dg:
            continue
        sim=len(qgrams & dg)/len(qgrams)
        if sim < 0.34:
            continue
        out.append({"path":r["path"],"source":r["source"],"snippet":content[:240],
                    "content":content,"updated_at":r["updated_at"] or "","fuzzy_sim":round(sim,4)})
    out.sort(key=lambda x: x["fuzzy_sim"], reverse=True)
    return out[:n_over]

def fuzzy_rerank(cands):
    out=[]
    for c in cands:
        content=c.get("content") or ""
        signals={
            "fuzzy_sim": c["fuzzy_sim"],
            "source_boost": source_boost(c["source"], content),
            "recency_boost": round(recency_boost(c.get("updated_at")), 4),
            "durability_penalty": durability_penalty(content),
            "usage_boost": usage_signal(content),
        }
        score=(c["fuzzy_sim"]*8.0)+signals["source_boost"]+signals["recency_boost"]+signals["durability_penalty"]+signals["usage_boost"]
        out.append({"path":c["path"],"source":c["source"],"snippet":c["snippet"],
                    "score":round(score,4),"signals":signals,"_chash":_chash(content)})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out

def rrf_fuse(lanes, k=60):
    # Reciprocal Rank Fusion: each lane contributes 1/(k+rank). Docs found by
    # multiple lanes accumulate; the richer per-doc record (more signals) is kept.
    agg={}
    for lane in lanes:
        for rank, item in enumerate(lane):
            p=item["path"]
            cur=agg.get(p)
            contrib=1.0/(k+rank+1)
            if cur is None:
                agg[p]={"item":item,"rrf":contrib,"nsig":len(item.get("signals",{}))}
            else:
                cur["rrf"]+=contrib
                if len(item.get("signals",{})) > cur["nsig"]:
                    cur["item"]=item; cur["nsig"]=len(item.get("signals",{}))
    fused=[]
    for v in agg.values():
        it=dict(v["item"])
        it["score"]=round(v["rrf"], 6)
        it.setdefault("signals", {})["rrf"]=round(v["rrf"], 6)
        fused.append(it)
    fused.sort(key=lambda x: x["score"], reverse=True)
    return fused[:limit]

def embed_query(text):
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

def cosine(a, b):
    if len(a) != len(b):
        return None
    dot=sum(x*y for x, y in zip(a, b))
    na=math.sqrt(sum(x*x for x in a)); nb=math.sqrt(sum(y*y for y in b))
    if na == 0 or nb == 0:
        return None
    return dot/(na*nb)

def embedding_scan(qvec, n_over):
    # Semantic lane: cosine of the query vector against the precomputed doc
    # vectors. Recalls synonyms / paraphrase / cross-language matches that the
    # lexical and fuzzy lanes (both surface-form) cannot.
    try:
        rows=con.execute("SELECT v.path AS path, v.vec AS vec, d.source AS source, "
                         "d.content AS content, d.updated_at AS updated_at "
                         "FROM memory_vectors v JOIN memory_docs d ON d.path = v.path").fetchall()
    except sqlite3.OperationalError:
        return []
    out=[]
    for r in rows:
        try:
            dv=json.loads(r["vec"])
        except Exception:
            continue
        sim=cosine(qvec, dv)
        if sim is None or sim < EMBED_MIN_SIM:
            continue
        content=r["content"] or ""
        out.append({"path":r["path"],"source":r["source"],"snippet":content[:240],
                    "content":content,"updated_at":r["updated_at"] or "","cos":round(sim,4)})
    out.sort(key=lambda x: x["cos"], reverse=True)
    return out[:n_over]

def embedding_rerank(cands):
    out=[]
    for c in cands:
        content=c.get("content") or ""
        signals={
            "cosine": c["cos"],
            "source_boost": source_boost(c["source"], content),
            "recency_boost": round(recency_boost(c.get("updated_at")), 4),
            "durability_penalty": durability_penalty(content),
            "usage_boost": usage_signal(content),
        }
        score=(c["cos"]*8.0)+signals["source_boost"]+signals["recency_boost"]+signals["durability_penalty"]+signals["usage_boost"]
        out.append({"path":c["path"],"source":c["source"],"snippet":c["snippet"],
                    "score":round(score,4),"signals":signals,"_chash":_chash(content)})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out

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
            "usage_boost": usage_signal(content),
        }
        # Simple local fusion. It is intentionally explainable and stdlib-only;
        # optional vector lanes can be added later without changing startup safety.
        score=(token_hits*4.0)+(phrase_hit*3.0)+signals["source_boost"]+signals["recency_boost"]+signals["durability_penalty"]+signals["usage_boost"]
        sn=content[:240]
        out.append({"path":r["path"],"source":r["source"],"snippet":sn,"score":round(score,4),"signals":signals,"_chash":_chash(content)})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]

toks = tokens_for(query)
rows=[]
requested = retrieval.strip().lower() or "fts"
used=[requested]
if requested in {"hybrid", "hybrid-local"}:
    mode = requested
    rows = hybrid(toks)
else:
    # Default: a lexical lane (FTS5 candidate generation + boost rerank; LIKE
    # fallback) fused with a stdlib fuzzy char-ngram lane via Reciprocal Rank
    # Fusion. The fuzzy lane recalls morphological / typo / partial-token matches
    # the exact FTS tokenizer misses (notably Korean surface-form variation),
    # without any model or network. Set CCC_MEMORY_FUSION=0 for the lexical lane
    # only. The fusion is the seam a future embedding lane would plug into.
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
    lexical = rerank(cands)
    fusion_on = os.environ.get("CCC_MEMORY_FUSION", "1").strip().lower() not in {"0","false","off","no"}
    lanes=[lexical]; used=["lexical"]
    if fusion_on:
        fuzzy = fuzzy_rerank(fuzzy_scan(char_ngrams(query), over))
        if fuzzy:
            lanes.append(fuzzy); used.append("fuzzy")
        if EMBED_CMD:
            qvec = embed_query(query)
            emb = embedding_rerank(embedding_scan(qvec, over)) if qvec is not None else []
            if emb:
                lanes.append(emb); used.append("embedding")
    if len(lanes) > 1:
        mode = "fusion-rrf"
        rows = rrf_fuse(lanes)
    else:
        mode = "fts-rerank"
        rows = lexical[:limit]

# Enforce the source boundary after every retrieval lane has fused. Individual
# lexical/fuzzy/embedding lanes may have read a stale pre-disable DB row.
if not wiki_enabled:
    rows = [row for row in rows if not hidden_by_wiki_boundary(row)]

# Feedback: record the surfaced docs (only when the caller opted in), then drop
# the internal content-hash before emitting results.
record_usage(rows)
for r in rows:
    r.pop("_chash", None)
print(json.dumps({"query":query,"tokens":toks,"retrievalMode":mode,"lanes":used,"results":rows}, ensure_ascii=False, indent=2))
