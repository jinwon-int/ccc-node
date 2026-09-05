#!/usr/bin/env python3
"""nunchi wiki-promote — weekly fleet-fact → wiki-candidates batch (#1447, #1264 P3-8).

Owner-approved design (2026-09-03, issue #1447 comment): a fleet-entity fact
that lives only in a node-local nunchi store has no reason to stay local — the
Family Wiki is the single source of truth (FW-05). Automatic Wiki writes are
forbidden, though, so the only legitimate path is
distill → wiki candidate → HUMAN review (the wiki-record flow). This batch
feeds eligible facts into that existing path; it never touches the Wiki.

Contract (all owner-approved, see the issue's design comments):
- shared scope ONLY. On audience-scoped nodes the parent pass enumerates the
  canonical `shared` child and re-runs per scope; private-* children are never
  opened (physical separation, not a filter — the immutable private boundary
  is the DB path itself). On unscoped nodes the fleet-roster filter is the
  privacy gate: session/peer/user observations fall out mechanically.
- mechanical eligibility (all AND, fixed order, first failure wins):
    1. observed ∈ fleet roster: nunchi.NODE_ALIASES canonical slugs ∪
       registered services (NUNCHI_WIKI_PROMOTE_SERVICES)
    2. kind ∈ {fact, decision, procedure} — constraint/preference are
       node-local rules, observation/context/task-progress are time-point
       data; a `decision` additionally requires a non-empty because (G5)
    3. mutability == 'static' (P1-4). The stored value and the derived
       nunchi._mutability(kind) must AGREE — disagreement is drift and fails
       closed. Note: legacy kind `fact` derives to live-check, so it fails
       here by construction; `retag` is its migration path.
    4. source_refs parses and carries session/transcript provenance (P1-3) —
       the reviewer must be able to backtrack the primary source
    5. valid_to IS NULL AND review=0 AND supersedes IS NULL
- body screen (defense-in-depth over distill redaction): local paths, token
  shapes, key material in fact/because → fail-closed exclusion (body-screen).
- 3-layer dedup: in-queue markers (fact id + normalized-text hash) → cheap
  substring match against the local Wiki cache (CCC_MEMORY_CACHE_DIR/wiki.txt;
  exact normalized substring — conservative by design, zero cost; its limits
  are absorbed by the human layer) → the human review layer itself.
- review-load cap: at most CAP candidates per run (default 5, oldest first —
  created_at ASC so the oldest insight cannot be starved forever); if the
  wiki-candidates queue holds more than BACKPRESSURE pending entries the
  whole run skips (reverse-pressure protection).
- idempotence: $NUNCHI_HOME/wiki-promoted.seen (permanent, NO TTL — unlike
  the wiki-queue .seen) records every queued fact id/hash; a rejected
  candidate must not re-queue forever.
- the nunchi store is opened READ-ONLY (sqlite mode=ro): review/supersede
  state is never mutated here. A promoted fact that is later superseded stays
  in the queue — the reviewer judges it (traceable via the fact-id marker).
- output: the wiki-queue.sh entry schema verbatim, appended to the SAME
  wiki-candidates queue file (no new transport path). Dry-run is the default;
  APPLY (NUNCHI_WIKI_PROMOTE_APPLY=1) is a separate, per-node owner approval
  materialized by install-nunchi.sh --wiki-promote-apply (#1270 precedent —
  the approval must survive installer replays, #1264 lesson).
- audit is body-free (verdict + reason + id only); the report carries no fact
  text.
- #873 lifecycle: this batch is a writer to an external durable surface (the
  wiki-candidates queue). Node decommission must include recovering pending
  entries — scan the queue for `nunchi-p3-8 fact#ID` markers of this node.

Audience-scoped nodes: the parent pass mirrors judge-batch.py's canonical
scope enumeration (the shape kept byte-identical across bench.sh /
piri-feed.sh / mempalace-refresh.sh), restricted to the `shared` child.
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
# Semantic contract, not a copy: the roster, mutability derivation, and the
# G5 decision-reason rule must mean exactly what nunchi.py means (#1204's
# drift lesson — a copied rule is a rule that drifts).
import nunchi  # noqa: E402
# #1508 — nunchi put the hooks root (~/.claude/hooks, where setup.sh installs
# bridge/utils/secure_fs.py as ccc_secure_fs.py) on sys.path, or registered the
# canonical repo module under that name, so the plain import resolves here.
import ccc_secure_fs  # noqa: E402

DB = os.environ.get("NUNCHI_DB", os.path.expanduser("~/.nunchi/facts.db"))
HOME_DIR = os.environ.get("NUNCHI_HOME", os.path.expanduser("~/.nunchi"))
STATE = os.environ.get("CCC_STATE_DIR", os.path.expanduser("~/.claude/state"))

QUEUE = os.path.join(STATE, "wiki-candidates.md")
SEEN = os.path.join(HOME_DIR, "wiki-promoted.seen")
AUDIT = os.path.join(HOME_DIR, "wiki-promote-audit.jsonl")
REPORT = os.path.join(STATE, "nunchi-wiki-promote-report.md")
LOCK = os.path.join(HOME_DIR, ".wiki-promote.lock")

# Verbatim wiki-queue.sh bootstrap header: this batch writes to the SAME queue
# with the SAME conventions, so a queue this batch creates must be
# indistinguishable from one the distill path created (#1447: 새 전송 경로
# 신설 금지).
QUEUE_HEADER = """# Wiki Candidates Queue (auto-generated by distill; review with `/wiki-record`)

> Each entry is a durable operational fact / decision proposed by the Session
> Distiller (TM-1058). Review and either:
>   - Promote via `/wiki-record` (creates PR), then mark status: merged below.
>   - Reject by deleting the entry (or marking status: rejected).
>
> Never auto-PR — this is a human-gated queue.

"""

APPLY = os.environ.get("NUNCHI_WIKI_PROMOTE_APPLY") == "1"

# Owner-approved default service roster (#1447 design point 1, 2026-09-03).
DEFAULT_SERVICES = ("searxng", "mempalace", "wiki-agent", "a2a-broker",
                    "telegram-web-bridge")
EXCHANGE_KINDS = ("fact", "decision", "procedure")
TRACEABLE_REF_TYPES = ("session", "transcript")


CAP = ccc_secure_fs.bounded_int_env(os.environ, "NUNCHI_WIKI_PROMOTE_CAP", 5, 1, 50, clamp=True)
BACKPRESSURE = ccc_secure_fs.bounded_int_env(os.environ, "NUNCHI_WIKI_PROMOTE_BACKPRESSURE", 20, 1, 200, clamp=True)

# Body screen — the second mechanical line behind distill redaction. Anything
# hit is EXCLUDED (fail-closed), never rewritten: a redaction mistake must not
# become the Wiki candidate's body. False exclusions are cheap (the human can
# queue the fact manually); false leaks are not.
BODY_SCREEN = (
    ("local-path", re.compile(
        r"(?<![\w./-])/(?:root|home|Users|mnt|srv|data)/")),
    ("token-like", re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{20,}"
        r"|gh[pousr]_[A-Za-z0-9]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}"
        r"|xox[baprs]-[A-Za-z0-9-]{10,}"
        r"|AKIA[0-9A-Z]{16})")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}", re.I)),
    ("url-credential", re.compile(
        r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")),
    ("long-hex", re.compile(r"\b[a-f0-9]{32,}\b", re.I)),
)

# Queue marker this batch embeds in every entry (reviewer traceability +
# in-queue dedup + #873 recovery scan key).
MARKER_RE = re.compile(
    r"nunchi-p3-8 fact#(\d+) scope=(\S+) hash=([0-9a-f]{12})")
_WS = re.compile(r"\s+")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Audience-scope fan-out — shared child ONLY (private is never opened)
# ---------------------------------------------------------------------------

def canonical_shared_scope(root):
    """The single canonical `shared` child of the audience root, or "".

    Same shape rules as judge-batch.py's enumerator: root and child must be
    directories owned by us with no group/other access. Anything else —
    including every private-* sibling — is never touched.
    """
    import stat as _stat
    try:
        meta = os.lstat(root)
    except OSError:
        return ""
    if not (os.path.isabs(root)
            and _stat.S_ISDIR(meta.st_mode)
            and meta.st_uid == os.geteuid()
            and not _stat.S_IMODE(meta.st_mode) & 0o077):
        return ""
    child = Path(root) / "shared"
    try:
        st = child.lstat()
    except OSError:
        return ""
    if not (_stat.S_ISDIR(st.st_mode)
            and st.st_uid == os.geteuid()
            and not _stat.S_IMODE(st.st_mode) & 0o077):
        return ""
    return str(child)


def fan_out_shared():
    """Re-run this script against the shared scope's fact store only."""
    root = os.environ.get("CCC_NUNCHI_AUDIENCE_ROOT", "")
    shared = canonical_shared_scope(root)
    if not shared:
        print(f"wiki-promote: no canonical shared scope under "
              f"{root or '(unset CCC_NUNCHI_AUDIENCE_ROOT)'} — nothing to do")
        return 0
    scope_db = os.path.join(shared, "nunchi", "facts.db")
    if not os.path.isfile(scope_db):
        print(f"wiki-promote: shared scope has no fact store ({scope_db})")
        return 0
    env = dict(os.environ)
    env["CCC_NUNCHI_SCOPED_CHILD"] = "1"
    env["CCC_NUNCHI_AUDIENCE_SCOPE"] = "shared"
    env["CCC_NUNCHI_AUDIENCE_KIND"] = "shared"
    env["NUNCHI_HOME"] = os.path.join(shared, "nunchi")
    env["NUNCHI_DB"] = scope_db
    env["NUNCHI_SNAPSHOT"] = os.path.join(shared, "nunchi", "snapshot.md")
    return subprocess.run(
        [sys.executable, os.path.abspath(__file__)], env=env).returncode


# ---------------------------------------------------------------------------
# Eligibility (mechanical, fixed order)
# ---------------------------------------------------------------------------

def services():
    raw = os.environ.get(
        "NUNCHI_WIKI_PROMOTE_SERVICES", ",".join(DEFAULT_SERVICES))
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def roster():
    return set(nunchi.NODE_ALIASES.values()) | services()


def parse_source_refs(raw):
    """Returns (rendered, types). rendered is None when unusable."""
    if not raw or not str(raw).strip():
        return None, set()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None, set()
    if not isinstance(parsed, list) or not parsed:
        return None, set()
    parts, types = [], set()
    for ref in parsed:
        if not isinstance(ref, dict):
            continue
        rtype = str(ref.get("type", "")).strip()
        val = ref.get("ref")
        if not rtype or val is None:
            continue
        types.add(rtype)
        entry = f"{rtype}: {val}"
        if ref.get("sha256_8"):
            entry += f" (sha256_8={ref['sha256_8']})"
        parts.append(entry)
    if not parts:
        return None, set()
    return "; ".join(parts)[:600], types


def body_screen_hit(text):
    if not text:
        return None
    for name, pattern in BODY_SCREEN:
        if pattern.search(text):
            return name
    return None


def check_row(row, fleet):
    """Fixed-order mechanical gate → (eligible?, reason)."""
    _fid, observed, kind, fact, because, source_refs, mutability, _created = row
    if observed not in fleet:
        return False, "observed-not-fleet"
    if kind not in EXCHANGE_KINDS:
        return False, "kind-not-exchangeable"
    if nunchi._g5_reasonless_decision(kind, fact, because):
        return False, "g5-reasonless-decision"
    derived = nunchi._mutability(kind)
    if mutability != "static" or derived != "static":
        return False, ("mutability-drift" if (mutability == "static"
                                              and derived != "static")
                       else "mutability-not-static")
    rendered, types = parse_source_refs(source_refs)
    if rendered is None:
        return False, "source-refs-missing"
    if not (set(types) & set(TRACEABLE_REF_TYPES)):
        return False, "source-refs-untraceable"
    hit = body_screen_hit(fact) or body_screen_hit(because)
    if hit:
        return False, f"body-screen:{hit}"
    return True, "eligible"


# ---------------------------------------------------------------------------
# Dedup layers
# ---------------------------------------------------------------------------

def normalize(text):
    return _WS.sub(" ", (text or "").strip().lower())


def normhash(text):
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:12]


def queue_state():
    """(pending_count, marker_ids, marker_hashes) from the shared queue."""
    ids, hashes, pending = set(), set(), 0
    try:
        text = Path(QUEUE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, ids, hashes
    for line in text.splitlines():
        if line.startswith("- status: pending"):
            pending += 1
        marker = MARKER_RE.search(line)
        if marker:
            ids.add(int(marker.group(1)))
            hashes.add(marker.group(3))
    return pending, ids, hashes


def load_seen():
    """Permanent idempotence ledger: {fact_id: hash}. No TTL by design —
    a rejected candidate must not re-queue every week."""
    rows = {}
    try:
        for line in Path(SEEN).read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0].isdigit():
                rows[int(parts[0])] = parts[1]
    except OSError:
        pass
    return rows


def wiki_cache_text():
    cache_dir = os.environ.get(
        "CCC_MEMORY_CACHE_DIR",
        os.path.expanduser("~/.claude/hooks/cache"))
    try:
        return Path(cache_dir, "wiki.txt").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""


def wiki_hit(fact, hay_norm):
    """Cheap layer-2 dedup: exact normalized substring against the local Wiki
    cache. Conservative (few false skips, some misses) and zero-cost; the
    human layer absorbs the imprecision, as the approved design states."""
    needle = normalize(fact)
    if len(needle) < 24 or not hay_norm:
        return False
    return needle in hay_norm


# ---------------------------------------------------------------------------
# Queue entry rendering (wiki-queue.sh schema verbatim + nunchi provenance)
# ---------------------------------------------------------------------------

def collapse(text, limit):
    flat = _WS.sub(" ", (text or "").strip())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip() + "…"


def suggested_path(observed):
    if observed in set(nunchi.NODE_ALIASES.values()):
        return f"pages/nodes/{observed}/"
    return f"pages/services/{observed}.md"


def next_cand_id(queue_text):
    last = 0
    for m in re.finditer(r"\[CAND-(\d+)\]", queue_text or ""):
        last = max(last, int(m.group(1)))
    return last + 1


def render_entry(cand_id, row, refs_rendered, digest, scope_tag):
    fid, observed, kind, fact, because, _src, _mut, created = row
    utcnow = datetime.now(timezone.utc)
    lines = [
        f"\n## [CAND-{cand_id}] {utcnow:%Y-%m-%d} — "
        f"{observed}: {collapse(fact, 72)}",
        f"- suggested-path: `{suggested_path(observed)}`",
        "- proposed-id: TM-?? (assign at PR time)",
        f"- source-session: `nunchi-wiki-promote/{scope_tag}` "
        "(trigger=#1447-p3-8)",
        f"- distilled-at: {utcnow:%Y-%m-%dT%H:%M:%SZ}",
        "- status: pending",
        f"- summary: {collapse(fact, 400)}",
        f"- source-fact: nunchi fact#{fid} observed={observed} kind={kind}"
        f" created={created}",
        f"- source-refs: {refs_rendered}",
    ]
    if kind == "decision" and (because or "").strip():
        lines.append(f"- because: {collapse(because, 200)}")
    lines.append(
        f"<!-- nunchi-p3-8 fact#{fid} scope={scope_tag} hash={digest} -->")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Apply + audit + report
# ---------------------------------------------------------------------------

def audit(entry):
    os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
    with open(AUDIT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_seen(entries):
    os.makedirs(HOME_DIR, exist_ok=True)
    with open(SEEN, "a", encoding="utf-8") as fh:
        for fid, digest, stamp in entries:
            fh.write(f"{fid} {digest} {stamp}\n")


def queue_text_or_header():
    try:
        return Path(QUEUE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return QUEUE_HEADER


def write_report(payload):
    os.makedirs(STATE, exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(payload)


def select_candidates(rows, fleet, seen, seen_hashes,
                      queued_ids, queued_hashes, hay_norm):
    """Fixed-order gate + dedup layers → (skipped, eligible).

    skipped is a list of (fid, reason); eligible is a list of
    (row, digest, refs_rendered) in oldest-first order.
    """
    skipped, eligible = [], []
    for row in rows:
        fid = row[0]
        ok, reason = check_row(row, fleet)
        if not ok:
            skipped.append((fid, reason))
            continue
        digest = normhash(row[3])
        if fid in seen or digest in seen_hashes:
            skipped.append((fid, "already-promoted"))
            continue
        if fid in queued_ids or digest in queued_hashes:
            skipped.append((fid, "already-queued"))
            continue
        if wiki_hit(row[3], hay_norm):
            skipped.append((fid, "wiki-cache-hit"))
            continue
        rendered, _types = parse_source_refs(row[5])
        eligible.append((row, digest, rendered))
    return skipped, eligible


def write_queue_entries(chosen, scope_tag, stamp):
    """APPLY: append entries + the seen ledger. Returns count queued."""
    try:
        existed = (os.path.getsize(QUEUE) > 0)
    except OSError:
        existed = False
    text = queue_text_or_header()
    cand_id = next_cand_id(text)
    blocks = []
    seen_rows = []
    for row, digest, rendered in chosen:
        blocks.append(
            render_entry(cand_id, row, rendered, digest, scope_tag))
        seen_rows.append((row[0], digest, stamp))
        cand_id += 1
    with open(QUEUE, "a", encoding="utf-8") as fh:
        # A queue this batch creates must carry the same bootstrap header the
        # distill path writes (same file, same conventions — #1447: 새 전송
        # 경로 신설 금지).
        if not existed:
            fh.write(QUEUE_HEADER)
        fh.write("".join(blocks))
    append_seen(seen_rows)
    return len(chosen)


def audit_and_report(rows, skipped, chosen, queued, overflow,
                     backpressure_skipped, pending, scope_tag, stamp):
    """Body-free audit lines + report file + cron-log line."""
    mode = "APPLY" if APPLY else "dry-run"
    for fid, reason in skipped:
        audit({"ts": stamp, "mode": mode, "db": DB, "scope": scope_tag,
               "id": fid, "verdict": "skip", "reason": reason})
    for row, _digest, _rendered in chosen:
        audit({"ts": stamp, "mode": mode, "db": DB, "scope": scope_tag,
               "id": row[0], "verdict": "candidate",
               "reason": "eligible",
               "applied": bool(APPLY and queued)})

    reasons = {}
    for _fid, reason in skipped:
        reasons[reason] = reasons.get(reason, 0) + 1
    skip_text = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
    report = [
        f"# nunchi wiki-promote report — {stamp}",
        "",
        f"- mode: **{mode}**"
        f" (NUNCHI_WIKI_PROMOTE_APPLY={'1' if APPLY else 'unset'})",
        f"- db: `{DB}` (read-only)",
        f"- scope: {scope_tag}",
        f"- open facts examined: {len(rows)}",
        f"- eligible candidates: {len(chosen) + max(overflow, 0)}"
        f" (cap {CAP}, overflow {max(overflow, 0)})",
        f"- queued: {queued}"
        + (" — run SKIPPED: queue backpressure"
           f" (pending {pending} > {BACKPRESSURE})"
           if backpressure_skipped else ""),
        f"- skips: {skip_text or 'none'}",
        "",
        "| id | verdict | reason |",
        "|---|---|---|",
    ]
    decided = [(fid, "skip", reason) for fid, reason in skipped]
    decided += [(row[0], "candidate",
                 "eligible" + (" · queued" if APPLY and queued
                               else " · dry-run only"))
                for row, _digest, _rendered in chosen]
    for fid, verdict, reason in sorted(decided):
        report.append(f"| #{fid} | {verdict} | {reason} |")
    write_report("\n".join(report) + "\n")

    print(f"wiki-promote ({mode}): examined={len(rows)},"
          f" candidates={len(chosen) + max(overflow, 0)},"
          f" queued={queued}"
          + (f", SKIPPED backpressure(pending={pending})"
             if backpressure_skipped else "")
          + (f", skips: {skip_text}" if skip_text else ""))


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
        scope_tag = os.environ.get("CCC_NUNCHI_AUDIENCE_SCOPE") or "unscoped"
        stamp = now()
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT id, observed, kind, fact, because, source_refs,"
            " mutability, created_at FROM peer_facts"
            " WHERE valid_to IS NULL AND review=0 AND supersedes IS NULL"
            " ORDER BY created_at ASC, id ASC").fetchall()
        conn.close()

        pending, queued_ids, queued_hashes = queue_state()
        seen = load_seen()
        skipped, eligible = select_candidates(
            rows, roster(), seen, set(seen.values()),
            queued_ids, queued_hashes, normalize(wiki_cache_text()))

        overflow = len(eligible) - CAP
        chosen = eligible[:CAP]
        for row, _digest, _rendered in eligible[CAP:]:
            skipped.append((row[0], "cap-overflow"))

        backpressure_skipped = pending > BACKPRESSURE
        if backpressure_skipped:
            for row, _digest, _rendered in chosen:
                skipped.append((row[0], "backpressure"))
            chosen = []

        queued = 0
        if APPLY and chosen:
            queued = write_queue_entries(chosen, scope_tag, stamp)

        audit_and_report(rows, skipped, chosen, queued, overflow,
                         backpressure_skipped, pending, scope_tag, stamp)
        return 0


def main():
    if (os.environ.get("CCC_NUNCHI_AUDIENCE_SCOPED") == "1"
            and os.environ.get("CCC_NUNCHI_SCOPED_CHILD") != "1"):
        return fan_out_shared()
    return run_single_db()


if __name__ == "__main__":
    sys.exit(main())
