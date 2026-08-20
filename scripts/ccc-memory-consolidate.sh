#!/usr/bin/env bash
# ccc-memory-consolidate.sh — collapse near-duplicate distilled facts.
#
# Over time memory-facts.jsonl accumulates restatements of the same thing — the
# distiller re-extracts a fact across sessions, or a mutable attribute is
# re-observed ("current editor is Helix" written five different ways). Exact-text
# dedup (local-facts.sh / the index) never catches these, so the same content is
# injected several times, crowding the budget and reading as contradictory.
#
# This pass clusters near-duplicate facts (character-4-gram Jaccard ≥ threshold,
# within the same LOGICAL KEY) and keeps the winner of each cluster, marking the
# losing MACHINE-GENERATED copies review:"superseded" — kept in the file as an
# audit trail, skipped by the index. Human-reviewed facts (review approved /
# needs-human) are never auto-superseded.
#
# #871 §4 supersede/conflict semantics. The contract is nunchi's, not a new one:
# nunchi.py's write gate already settled these questions for the peer-facts lane
# and both lanes must mean the same thing, or an audit that reads one and
# reasons about the other is wrong.
#
#   Logical key (kind, subject)  Lexical similarity alone is not identity. Two
#       facts about DIFFERENT subjects that happen to share phrasing must never
#       collapse into one, so clustering is partitioned by (kind, subject) and
#       similarity only decides membership inside a partition. subject comes
#       from entities[0]; a fact without one clusters only with other
#       subject-less facts of its kind.
#
#   G2 source precedence  A lower-rank fact never closes a higher-rank one — an
#       agent inference must not bury a user statement. Rank (3 user-stated /
#       2 measured / 1 inferred) is read from source_rank and is a SEPARATE axis
#       from confidence; the winner is the highest rank, newest only as the
#       tiebreak. Unset/unparseable rank is 1, matching G2's demote-don't-trust
#       rule for an unverifiable claim.
#
#   G3 ambiguity is never auto-resolved  If the NEWEST fact in a cluster ranks
#       below the winner, the cluster is a real contradiction — newer-but-weaker
#       against older-but-stronger — and neither direction is safely decidable:
#       superseding the old one buries a user statement, superseding the new one
#       discards current information. So nothing in that cluster is superseded,
#       the newcomer is flagged review:"needs-human", and both stay open and
#       visible. This is the only path that MINTS needs-human; before it the
#       script merely respected one already set by a human.
#
#   valid_until on the loser  Marking review:"superseded" alone left the slice-1
#       temporal search (current/as_of, #1197) unable to act on the decision:
#       it reads valid_from/valid_until, not review. A superseded fact now also
#       gets valid_until = the winner's observed_at and superseded_by = the
#       winner's id, which is what actually connects the two slices — the fact
#       stops being "current" and stays retrievable as of a past instant.
#
# Local-only, atomic, bounded, fail-open. Best run from the background memory
# refresh (network-allowed, off the hot path); it never blocks startup.
set -uo pipefail

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
FACTS_FILE="${CCC_MEMORY_FACTS_FILE:-$STATE_DIR/memory-facts.jsonl}"
SIM="${CCC_MEMORY_CONSOLIDATE_SIM:-0.82}"
mkdir -p "$STATE_DIR" 2>/dev/null || true

is_disabled() { case "${1:-}" in 0|false|FALSE|off|OFF|no|NO) return 0;; *) return 1;; esac; }

# Off-switches: the consolidate-specific flag and the shared distill kill-switch.
if is_disabled "${CCC_MEMORY_CONSOLIDATE:-1}"; then
  echo '{"ok":true,"skipped":"disabled"}'; exit 0
fi
[ -f "$STATE_DIR/distill.disabled" ] && { echo '{"ok":true,"skipped":"distill-disabled"}'; exit 0; }
[ -f "$FACTS_FILE" ] || { echo '{"ok":true,"skipped":"no-facts-file"}'; exit 0; }

FACTS_FILE="$FACTS_FILE" SIM="$SIM" python3 - <<'PY' || { echo '{"ok":false,"error":"consolidate-failed"}'; exit 0; }
import fcntl, json, os, re, sys, tempfile, time

facts_file = os.environ["FACTS_FILE"]
try:
    sim_threshold = float(os.environ.get("SIM", "0.82") or 0.82)
except ValueError:
    sim_threshold = 0.82

MIN_LEN = 12          # don't aggressively cluster very short facts
NGRAM = 4
# Only machine-generated facts may be auto-superseded; human-touched stay put.
SUPERSEDABLE = {"", "auto-local", "auto"}

def norm(t):
    return " ".join(re.findall(r"[0-9a-z가-힣]+", (t or "").lower()))

def ngrams(s):
    chars = re.findall(r"[0-9a-z가-힣]", (s or "").lower())
    n = "".join(chars)
    if len(n) < NGRAM:
        return {n} if n else set()
    return {n[i:i+NGRAM] for i in range(len(n) - NGRAM + 1)}

def jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)

# Serialize against the appenders before reading.  This pass is a read ->
# cluster -> os.replace of the WHOLE file, so a distiller append landing inside
# that window was overwritten and lost.  CodexLocalMemorySink holds this exact
# lock across its own read-modify-write (bridge/memory/distill_local_sink.py),
# and CodexMemoryPromoter coordinates on it too, so it is the file's canonical
# mutex -- taking anything else would not actually exclude them.  Mode 0600 and
# O_NOFOLLOW match the sink's lock validation; a differently-moded lock file
# would make the sink fail closed.
lock_path = os.path.join(os.path.dirname(facts_file) or ".", ".local-memory-sink.lock")
lock_fd = None
try:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock_path, flags, 0o600)
    os.fchmod(lock_fd, 0o600)
    for attempt in range(5):
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if attempt == 4:
                # Fail open, the file's whole contract: a writer is active, so
                # skip this pass rather than block the background refresh.
                print(json.dumps({"ok": True, "skipped": "locked"})); sys.exit(0)
            time.sleep(0.2)
except SystemExit:
    raise
except Exception:
    print(json.dumps({"ok": False, "error": "lock-failed"})); sys.exit(0)
# The fd is intentionally left open: the flock is released when this
# short-lived process exits, which is exactly the window we must cover.

lines = []
try:
    with open(facts_file, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            if raw.strip():
                lines.append(raw)
except Exception:
    print(json.dumps({"ok": False, "error": "read-failed"})); sys.exit(0)

def source_rank(obj):
    """G2 rank, defaulting to 1 (inferred).

    Anything we cannot read as 1/2/3 is treated as the weakest rank rather
    than trusted: G2 demotes an unverifiable claim instead of honouring it,
    and a malformed value is exactly that.
    """
    try:
        r = int(obj.get("source_rank"))
    except (TypeError, ValueError):
        return 1
    return r if 1 <= r <= 3 else 1


def subject_of(obj):
    """Logical-key subject — entities[0], normalized. '' when absent."""
    ents = obj.get("entities")
    if isinstance(ents, list) and ents:
        return norm(str(ents[0]))
    return ""


records = []  # (idx, obj, text, review, key, grams, sort_key, rank)
for idx, raw in enumerate(lines):
    try:
        obj = json.loads(raw)
    except Exception:
        obj = None
    if not isinstance(obj, dict):
        records.append((idx, None, None, None, None, None, None, None))
        continue
    review = str(obj.get("review") or "auto-local").lower()
    text = str(obj.get("text") or obj.get("summary") or "")
    kind = str(obj.get("kind") or "fact").lower()
    # Logical key, not kind alone: same-kind facts about different subjects are
    # different facts however similar their wording.
    key = (kind, subject_of(obj))
    grams = ngrams(text)
    # Recency: prefer observed_at, then file order (later = newer).
    sort_key = (str(obj.get("observed_at") or ""), idx)
    records.append((idx, obj, text, review, key, grams, sort_key, source_rank(obj)))

# Candidates eligible to participate in clustering (already-inert facts excluded).
elig = [r for r in records
        if r[1] is not None and r[3] not in ("rejected", "superseded")
        and r[2] and len(r[2]) >= MIN_LEN and r[5]]

# Union-find over near-duplicate pairs within the same kind.
parent = {r[0]: r[0] for r in elig}
def find(x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x
def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[rb] = ra

for i in range(len(elig)):
    for j in range(i + 1, len(elig)):
        a, b = elig[i], elig[j]
        if a[4] != b[4]:
            continue
        if jaccard(a[5], b[5]) >= sim_threshold:
            union(a[0], b[0])

clusters = {}
for r in elig:
    clusters.setdefault(find(r[0]), []).append(r)

by_idx = {r[0]: r for r in records}
superseded_idx = {}   # idx -> winner record
conflict_idx = set()
cluster_count = 0
for members in clusters.values():
    if len(members) < 2:
        continue
    cluster_count += 1
    # G2: highest rank wins; recency is only the tiebreak. This is what stops
    # an inference from closing a user statement.
    keeper = max(members, key=lambda r: (r[7], r[6]))
    newest = max(members, key=lambda r: r[6])
    if newest[7] < keeper[7]:
        # G3: newer-but-weaker vs older-but-stronger. Not auto-resolvable in
        # either direction, so resolve nothing — flag and leave both open.
        # Only an untouched machine fact is flagged, which also makes the pass
        # idempotent: once it reads needs-human it is no longer SUPERSEDABLE.
        if newest[3] in SUPERSEDABLE:
            conflict_idx.add(newest[0])
        continue
    for r in members:
        if r[0] == keeper[0]:
            continue
        if r[3] in SUPERSEDABLE:  # only auto-generated copies are demoted
            superseded_idx[r[0]] = keeper

if not superseded_idx and not conflict_idx:
    print(json.dumps({"ok": True, "total": len(lines), "clusters": cluster_count,
                      "superseded": 0, "conflicts": 0, "changed": False}))
    sys.exit(0)

out_lines = []
for idx, raw in enumerate(lines):
    if idx in superseded_idx:
        obj = dict(by_idx[idx][1])
        obj["review"] = "superseded"
        winner = superseded_idx[idx]
        # Connect the decision to the slice-1 temporal search, which reads
        # valid_until rather than review. Never widen an existing bound: a
        # valid_until already on the fact was set deliberately.
        if not obj.get("valid_until"):
            winner_at = str((winner[1] or {}).get("observed_at") or "")
            if winner_at:
                obj["valid_until"] = winner_at
        winner_id = str((winner[1] or {}).get("id") or "")
        if winner_id:
            obj["superseded_by"] = winner_id
        out_lines.append(json.dumps(obj, ensure_ascii=False))
    elif idx in conflict_idx:
        obj = dict(by_idx[idx][1])
        obj["review"] = "needs-human"
        obj["conflict"] = "source-rank"
        out_lines.append(json.dumps(obj, ensure_ascii=False))
    else:
        out_lines.append(raw)

d = os.path.dirname(facts_file) or "."
try:
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".memory-facts.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out_lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, facts_file)
except Exception:
    print(json.dumps({"ok": False, "error": "write-failed"})); sys.exit(0)

print(json.dumps({"ok": True, "total": len(lines), "clusters": cluster_count,
                  "superseded": len(superseded_idx),
                  "conflicts": len(conflict_idx), "changed": True}))
PY
