#!/usr/bin/env python3
"""nunchi wiki-promote — weekly fleet-fact Wiki promotion batch (#1447, #1264 P3-8).

Design contract (issue #1447, owner-approved 2026-09-03 — all five decision
points recorded in the design-confirmation comment):

Fleet-entity facts (`observed` = canon node slug or registered service) have no
reason to live only in a node-local store — the Family Wiki is the single
source of truth (FW-05). Automatic Wiki writes are forbidden, so the sanctioned
path is distill → wiki candidate → HUMAN review (wiki-record flow). This batch
is the periodic feeder for that path; it never writes to the Wiki itself.

Privacy boundary is PHYSICAL, not filter-based:
- audience-scoped nodes: the parent pass enumerates canonical scope children
  and re-runs ONLY the `shared` scope child. Private scope stores are never
  opened by this script (a private fact cannot leak through code it never
  reads). Non-scoped nodes have one store where the fleet-roster filter is the
  gate: session:* peer facts and user-peer observations mechanically fail the
  roster check, so private-natured facts are never candidates.

Mechanical eligibility (all AND, #1437/#1439 assets):
- observed in the fleet roster (NODE_ALIASES slugs ∪ registered services —
  imported from nunchi.py, never copied, so the roster cannot drift)
- kind in {fact, decision, procedure} (constraint excluded per design; legacy
  `fact` rows derive to live-check via P1-4 and fall to the mutability gate)
- mutability='static' (live-check operational facts stay node-local, #1439)
- source_refs present (#1437 — reviewer can backtrack session/transcript)
- valid_to IS NULL, review=0, supersedes IS NULL
- decision requires a non-empty `because` (G5, #1264)
- secondary machine screen: local absolute paths or token-shaped strings in the
  fact text are fail-closed EXCLUDED (defense-in-depth under the human review)

Dedup, three layers (#1447 design):
1. queue + seen ledger: HTML marker `<!-- nunchi-p3-8 fact#ID h=<hash> -->` in
   any queue entry (any status) skips the fact id/hash; the scope-local seen
   ledger (wiki-promoted.seen, NO TTL) permanently suppresses re-queueing
2. wiki cache substring: normalized fact text found in the local wiki.txt
   cache skips the candidate (cheap layer; misses are absorbed by the human)
3. human review: the wiki-record promotion flow is and stays the final gate

Safety rails:
- the fact store is opened READ-ONLY (sqlite URI mode=ro) — no review/supersede
  state changes in any mode; promotion state lives in the queue + ledger
- dry-run by default (report only); NUNCHI_WIKI_PROMOTE_APPLY=1 writes queue
  entries — a separate, per-node owner approval (#1270 judge-batch precedent);
  cron activation of APPLY is a further separate approval (#1447 design note)
- backpressure: more than NUNCHI_WIKI_PROMOTE_BACKPRESSURE pending queue
  entries skips the run entirely
- CAP candidates per run (oldest first — created_at ASC so old insights are not
  starved), flock against concurrent runs, body-free report + audit log

External-store note (#873 lifecycle contract): this batch is a WRITER of
external-bound storage (the wiki-candidates queue feeds the Family Wiki).
Its local artifacts are registered in schemas/memory-artifact-inventory.v1.json
(nunchi.wiki_promote_seen, nunchi.wiki_promote_audit; the queue itself is
covered by distill.wiki_candidates). Node decommission must surface
promotion-candidate recovery through the #873 handoff slice, not a bare rm.
"""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Semantic contracts imported, never copied: the fleet roster (NODE_ALIASES)
# and the mutability derivation (_mutability) must mean exactly what nunchi.py
# means — a copied roster would silently drift as nodes join/leave (#1204
# lesson, mirrored for the roster by the #1447 design).
import nunchi  # noqa: E402

DB = os.environ.get("NUNCHI_DB", os.path.expanduser("~/.nunchi/facts.db"))
HOME_DIR = os.environ.get("NUNCHI_HOME", os.path.expanduser("~/.nunchi"))
STATE = os.environ.get("CCC_STATE_DIR", os.path.expanduser("~/.claude/state"))
CACHE_DIR = os.environ.get("CCC_MEMORY_CACHE_DIR", os.path.expanduser("~/.claude/hooks/cache"))
QUEUE = os.path.join(STATE, "wiki-candidates.md")
WIKI_CACHE = os.path.join(CACHE_DIR, "wiki.txt")
SEEN = os.path.join(HOME_DIR, "wiki-promoted.seen")
AUDIT = os.path.join(HOME_DIR, "wiki-promote-audit.jsonl")
REPORT = os.path.join(STATE, "nunchi-wiki-promote-report.md")
LOCK = os.path.join(HOME_DIR, ".wiki-promote.lock")

APPLY = os.environ.get("NUNCHI_WIKI_PROMOTE_APPLY") == "1"

# Owner-confirmed registered-service roster (#1447 design confirmation).
DEFAULT_SERVICES = (
    "searxng", "mempalace", "wiki-agent", "a2a-broker", "telegram-web-bridge",
)

KINDS = ("fact", "decision", "procedure")
MARKER = "nunchi-p3-8"
WIKI_CACHE_MAX_BYTES = 2 * 1024 * 1024


def _int_env(name, default, low, high):
    try:
        val = int(os.environ.get(name, str(default)))
    except ValueError:
        val = default
    return max(low, min(high, val))


CAP = _int_env("NUNCHI_WIKI_PROMOTE_CAP", 5, 1, 50)
BACKPRESSURE = _int_env("NUNCHI_WIKI_PROMOTE_BACKPRESSURE", 20, 1, 500)
MAX_SCOPES = _int_env("CCC_NUNCHI_MAX_SCOPES_PER_RUN", 64, 1, 64)
# Dedup runs post-query, so oversample the SQL pull to still fill the cap
# after roster/screen/dedup exclusions (bounded; the store is node-local).
OVERSAMPLE = min(CAP * 4, 200)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Privacy screen (fail-closed) — secondary machine layer under human review
# ---------------------------------------------------------------------------

_LOCAL_PATH_RE = re.compile(r"(?<![\w`])/(?:home|root|Users)/[^\s`'\"]{2,}")
_TOKEN_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|gho_[A-Za-z0-9]{20,}"
    r"|xox[bpars]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|(?<![\w.])[0-9a-f]{32,}(?![\w.]))"
)
_SECRET_ASSIGN_RE = re.compile(
    r"\b(?:api[_-]?key|secret|password|passphrase|token|bearer)\s*[:=]\s*\S{4,}",
    re.IGNORECASE,
)


def privacy_hit(*texts):
    """True when any text carries a local path or token-shaped string."""
    for text in texts:
        if not text:
            continue
        if _LOCAL_PATH_RE.search(text) or _TOKEN_RE.search(text) \
                or _SECRET_ASSIGN_RE.search(text):
            return True
    return False


# ---------------------------------------------------------------------------
# Normalization + hash (wiki-queue.sh title_hash 방식 — two-tier, #1447)
# ---------------------------------------------------------------------------

def normalize_text(text):
    """wiki-queue.sh title_hash normalization, ported: issue-anchored strings
    keep their own bucket; everything else is sigilless-collapsed."""
    issues = sorted(set(re.findall(r"#([0-9]+)", text)), key=int)
    if issues:
        return "i" + "-".join(issues)
    t = text.lower()
    t = re.sub(r"^\s*(issue|이슈|결정|정책|런북|runbook|decision|spec|policy):\s+", "", t)
    for ch in "—–：(),./?!":
        t = t.replace(ch, " ")
    t = t.replace(":", " ")
    t = re.sub(r"(^| )r[0-9]+( |$)", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def fact_hash(text):
    normalized = normalize_text(text)
    if not normalized:
        normalized = "empty"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def entry_title(fact):
    first_line = fact.splitlines()[0].strip().lstrip("#").strip()
    if len(first_line) > 96:
        cut = first_line[:96].rsplit(" ", 1)[0].strip()
        first_line = (cut or first_line[:96]) + "…"
    return first_line or "(untitled fact)"


# ---------------------------------------------------------------------------
# Audience-scope fan-out — shared scope ONLY (physical privacy boundary)
# ---------------------------------------------------------------------------

def canonical_scope_children(root, limit):
    """Canonical direct children of the opaque audience root.

    Same enumerator contract as judge-batch.py/bench.sh: root and each child
    must be a directory owned by us with no group/other access; names are
    'shared' or 'private-<32 lowercase hex>'; sorted, capped.
    """
    import stat as _stat
    try:
        meta = os.lstat(root)
    except OSError:
        return []
    if not (os.path.isabs(root)
            and _stat.S_ISDIR(meta.st_mode)
            and meta.st_uid == os.geteuid()
            and not _stat.S_IMODE(meta.st_mode) & 0o077):
        return []
    out = []
    for child in sorted(Path(root).iterdir(), key=lambda p: p.name):
        if len(out) >= limit:
            break
        if child.name != "shared" and not re.fullmatch(r"private-[0-9a-f]{32}", child.name):
            continue
        try:
            st = child.lstat()
        except OSError:
            continue
        if not (_stat.S_ISDIR(st.st_mode)
                and st.st_uid == os.geteuid()
                and not _stat.S_IMODE(st.st_mode) & 0o077):
            continue
        out.append(str(child))
    return out


def fan_out_scopes():
    """Re-run for the shared scope only; private scopes are never opened."""
    root = os.environ.get("CCC_NUNCHI_AUDIENCE_ROOT", "")
    rc = 0
    ran = False
    for scope_root in canonical_scope_children(root, MAX_SCOPES):
        if os.path.basename(scope_root) != "shared":
            continue  # physical boundary — private stores stay closed (#1447)
        scope_db = os.path.join(scope_root, "nunchi", "facts.db")
        if not os.path.isfile(scope_db):
            continue
        ran = True
        env = dict(os.environ)
        env["CCC_NUNCHI_SCOPED_CHILD"] = "1"
        env["CCC_NUNCHI_AUDIENCE_SCOPE"] = "shared"
        env["CCC_NUNCHI_AUDIENCE_KIND"] = "shared"
        env["NUNCHI_HOME"] = os.path.join(scope_root, "nunchi")
        env["NUNCHI_DB"] = scope_db
        proc = subprocess.run([sys.executable, os.path.abspath(__file__)], env=env)
        rc = rc or proc.returncode
    if not ran:
        print("wiki-promote: no shared-scope fact store found — nothing to do")
    return rc


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def fleet_roster():
    """NODE_ALIASES slugs ∪ registered services (env-overridable list)."""
    services_raw = os.environ.get("NUNCHI_WIKI_PROMOTE_SERVICES", "").strip()
    if services_raw:
        services = [s.strip() for s in services_raw.split(",") if s.strip()]
    else:
        services = list(DEFAULT_SERVICES)
    return set(nunchi.NODE_ALIASES.values()) | set(services)


def fetch_candidates(conn):
    """SQL pre-gate mirrors the mechanical eligibility; Python re-verifies.

    Opened via a read-only URI by the caller: the connection structurally
    cannot write, in any mode (dry-run or APPLY). A pre-P1-3 store (missing
    mutability/source_refs columns) surfaces as an OperationalError blocker —
    migrating it would be a write, which this script never does (#1447).
    """
    return conn.execute(
        "SELECT id, observed, kind, fact, because, created_at, source_refs"
        " FROM peer_facts"
        " WHERE valid_to IS NULL AND review=0 AND supersedes IS NULL"
        " AND mutability='static'"
        " AND source_refs IS NOT NULL AND source_refs != ''"
        " ORDER BY created_at ASC, id ASC LIMIT ?",
        (OVERSAMPLE,),
    ).fetchall()
    # Deliberately NO kind filter here: the KINDS gate in classify_candidates
    # owns kind semantics (imported contract), so the report's kind-excluded
    # counts stay truthful instead of silently vanishing into the SQL layer.


def classify_candidates(rows, roster, pending_ids, pending_hashes, seen_hashes,
                        wiki_norm, counts):
    """Mechanical gates in fixed order; first failure wins the class label."""
    def exclude(label):
        counts[label] = counts.get(label, 0) + 1

    selected = []
    for fid, observed, kind, fact, because, created, refs in rows:
        if observed not in roster:
            exclude("roster-miss")
            continue
        if kind not in KINDS:
            exclude("kind-excluded")
            continue
        if nunchi._mutability(kind) != "static":
            exclude("mutability-excluded")
            continue
        if kind == "decision" and not (because or "").strip():
            exclude("g5-reasonless-decision")
            continue
        if privacy_hit(fact, because):
            exclude("screen-privacy")
            continue
        digest = fact_hash(fact)
        if fid in pending_ids:
            exclude("queue-dupe-id")
            continue
        if digest in pending_hashes:
            exclude("queue-dupe-hash")
            continue
        if digest in seen_hashes:
            exclude("seen-dupe")
            continue
        if wiki_norm is not None:
            norm = normalize_text(fact)
            if norm and norm in wiki_norm:
                exclude("wiki-cache-hit")
                continue
        if len(selected) >= CAP:
            exclude("cap-deferred")
            continue
        selected.append({
            "id": fid, "observed": observed, "kind": kind,
            "fact": fact, "because": (because or "").strip(),
            "created_at": created, "source_refs": refs, "hash": digest,
        })
    return selected


def first_session_ref(refs_json):
    """Extract the first session-type ref for the source-session line."""
    try:
        refs = json.loads(refs_json)
    except (TypeError, ValueError):
        return None
    if isinstance(refs, dict):
        refs = [refs]
    if not isinstance(refs, list):
        return None
    for ref in refs:
        if isinstance(ref, dict) and ref.get("type") == "session" and ref.get("ref"):
            return str(ref["ref"])
    return None


# ---------------------------------------------------------------------------
# Queue + ledger state
# ---------------------------------------------------------------------------

def read_queue_text():
    try:
        with open(QUEUE, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def queue_markers(queue_text):
    """All nunchi marker ids/hashes in the queue, regardless of entry status —
    a merged promotion must not re-queue, and a hash hit covers re-processed
    fact texts whose row id changed."""
    ids, hashes = set(), set()
    for match in re.finditer(
            rf"<!--\s*{re.escape(MARKER)}\s+fact#(\d+)(?:\s+h=([0-9a-f]{{12}}))?\s*-->",
            queue_text):
        ids.add(int(match.group(1)))
        if match.group(2):
            hashes.add(match.group(2))
    return ids, hashes


def pending_count(queue_text):
    return sum(1 for line in queue_text.splitlines()
               if line.strip() == "- status: pending")


def load_seen():
    hashes = set()
    try:
        with open(SEEN, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) == 4:
                    hashes.add(parts[3])
    except FileNotFoundError:
        pass
    return hashes


def update_seen(digest):
    """4-column ledger row, same shape as wiki-candidates.seen — but NO TTL:
    rows are never pruned by this script (permanent re-queueing suppressor)."""
    epoch = int(datetime.now(timezone.utc).timestamp())
    row = f"{epoch} {epoch} 1 {digest}\n"
    existing = ""
    try:
        with open(SEEN, encoding="utf-8") as fh:
            existing = fh.read()
    except FileNotFoundError:
        pass
    if any(len(parts) == 4 and parts[3] == digest
           for parts in (line.split() for line in existing.splitlines())):
        return
    tmp = SEEN + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(existing + row)
    os.replace(tmp, SEEN)


# ---------------------------------------------------------------------------
# Apply (queue write only — the fact store stays read-only)
# ---------------------------------------------------------------------------

def next_cand_id(queue_text):
    ids = [int(m) for m in re.findall(r"\[CAND-([0-9]+)\]", queue_text)]
    return (max(ids) + 1) if ids else 1


def build_entry(cand, cand_id, ts_log, date):
    session = first_session_ref(cand["source_refs"]) or "nunchi-facts"
    suggested = (f"pages/services/{cand['observed']}.md"
                 if cand["observed"] in fleet_roster_services_only()
                 else f"pages/nodes/{cand['observed']}/facts.md")
    lines = [
        "",
        f"## [CAND-{cand_id}] {date} — {entry_title(cand['fact'])}",
        f"<!-- {MARKER} fact#{cand['id']} h={cand['hash']} -->",
        f"- suggested-path: `{suggested}`",
        "- proposed-id: TM-?? (assign at PR time)",
        f"- source-session: `{session}` (trigger=nunchi-weekly)",
        f"- distilled-at: {ts_log}",
        "- status: pending",
        f"- summary: {cand['fact']}",
    ]
    evidence = [f"source_refs: {cand['source_refs']}"]
    if cand["because"]:
        evidence.append(f"because: {cand['because']}")
    lines.append("- evidence-excerpt: |")
    lines.extend(f"    {line}" for line in evidence)
    return "\n".join(lines) + "\n"


_FLEET_SERVICES_CACHE = None


def fleet_roster_services_only():
    global _FLEET_SERVICES_CACHE
    if _FLEET_SERVICES_CACHE is None:
        services_raw = os.environ.get("NUNCHI_WIKI_PROMOTE_SERVICES", "").strip()
        if services_raw:
            _FLEET_SERVICES_CACHE = {s.strip() for s in services_raw.split(",") if s.strip()}
        else:
            _FLEET_SERVICES_CACHE = set(DEFAULT_SERVICES)
    return _FLEET_SERVICES_CACHE


def append_entries(entries):
    os.makedirs(STATE, exist_ok=True)  # O_CREAT makes the file, not the parents
    block = "".join(entries)
    fd = os.open(QUEUE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(block)


# ---------------------------------------------------------------------------
# Report + audit (body-free: ids, classes, reasons — never fact text)
# ---------------------------------------------------------------------------

def audit(entry):
    os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
    with open(AUDIT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_report(payload):
    os.makedirs(STATE, exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(payload)


def build_report(stamp, mode, blocker, eligible, counts, backpressure_skipped, applied):
    lines = [
        f"# nunchi wiki-promote report — {stamp}",
        "",
        f"- mode: **{mode}** (NUNCHI_WIKI_PROMOTE_APPLY={'1' if APPLY else 'unset'})",
        f"- db: `{DB}` (opened read-only)",
        f"- queue: `{QUEUE}`",
        f"- cap: {CAP} · backpressure limit: {BACKPRESSURE}",
    ]
    if blocker:
        lines.append(f"- blocker: {blocker}")
    if backpressure_skipped:
        lines.append("- run skipped: wiki-candidates pending count above the backpressure limit")
    lines.append(f"- candidates selected: {len(eligible)}"
                 + (f" · queued: {applied}" if APPLY else " (dry-run — nothing written)"))
    if counts:
        lines.append("- exclusions: "
                     + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if eligible:
        lines += ["", "| fact id | observed | kind | decision |", "|---|---|---|---|"]
        for cand in eligible:
            if not APPLY:
                decision = "would queue"
            else:
                decision = "queued" if cand.get("queued") else "not queued"
            lines.append(
                f"| #{cand['id']} | {cand['observed']} | {cand['kind']} | {decision} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_single_db():
    if not os.path.isfile(DB):
        print(f"wiki-promote: no fact store at {DB} — nothing to do")
        return 0
    os.makedirs(HOME_DIR, exist_ok=True)
    with open(LOCK, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("wiki-promote: another run holds the lock — skipping")
            return 0
        blocker = ""
        eligible = []
        counts = {}
        backpressure_skipped = False
        applied = 0
        try:
            conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            blocker = f"db-open-failed ({exc})"
            conn = None
        if conn is not None:
            try:
                rows = fetch_candidates(conn)
            except sqlite3.OperationalError as exc:
                rows = []
                blocker = ("schema-pre-P1-3 — run 'nunchi.py init' to migrate; "
                           f"this batch never migrates ({exc})")
            conn.close()
            if not blocker:
                queue_text = read_queue_text()
                if pending_count(queue_text) > BACKPRESSURE:
                    backpressure_skipped = True
                else:
                    pending_ids, pending_hashes = queue_markers(queue_text)
                    wiki_norm = None
                    try:
                        size = os.path.getsize(WIKI_CACHE)
                        with open(WIKI_CACHE, encoding="utf-8", errors="replace") as fh:
                            wiki_raw = fh.read(min(size, WIKI_CACHE_MAX_BYTES))
                        wiki_norm = re.sub(r"\s+", " ", wiki_raw.lower())
                    except OSError:
                        wiki_norm = None  # cache layer is best-effort by design
                    counts = {}
                    eligible = classify_candidates(
                        rows, fleet_roster(), pending_ids, pending_hashes,
                        load_seen(), wiki_norm, counts)
                    if APPLY and eligible:
                        queue_text = read_queue_text()
                        cand_id = next_cand_id(queue_text)
                        ts_log = now()
                        date = ts_log[:10]
                        entries = []
                        for cand in eligible:
                            entries.append(build_entry(cand, cand_id, ts_log, date))
                            cand["queued"] = True
                            cand_id += 1
                        append_entries(entries)
                        for cand in eligible:
                            update_seen(cand["hash"])
                        applied = len(eligible)
        mode = "APPLY" if APPLY else "dry-run"
        stamp = now()
        for cand in eligible:
            audit({
                "ts": stamp, "db": DB, "id": cand["id"],
                "observed": cand["observed"], "kind": cand["kind"],
                "class": "promote-candidate",
                "applied": bool(APPLY and cand.get("queued")),
                "hash": cand["hash"],
            })
        if blocker or backpressure_skipped:
            audit({
                "ts": stamp, "db": DB, "id": None,
                "class": "blocker" if blocker else "backpressure-skip",
                "detail": blocker or f"pending>{BACKPRESSURE}",
                "applied": False,
            })
        write_report(build_report(
            stamp, mode, blocker, eligible, counts, backpressure_skipped, applied))
        print(f"wiki-promote ({mode}): selected={len(eligible)}"
              f" queued={applied if APPLY else 0}"
              + (f" blocker={blocker}" if blocker else "")
              + (" backpressure-skip" if backpressure_skipped else ""))
        return 0


def main():
    if (os.environ.get("CCC_NUNCHI_AUDIENCE_SCOPED") == "1"
            and os.environ.get("CCC_NUNCHI_SCOPED_CHILD") != "1"):
        return fan_out_scopes()
    return run_single_db()


if __name__ == "__main__":
    sys.exit(main())
