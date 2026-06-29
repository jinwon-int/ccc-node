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
# within the SAME kind) and keeps the most recent of each cluster, marking the
# older MACHINE-GENERATED copies review:"superseded" — kept in the file as an
# audit trail, skipped by the index. Human-reviewed facts (review approved /
# needs-human) are never auto-superseded.
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
import json, os, re, sys, tempfile

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

lines = []
try:
    with open(facts_file, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            if raw.strip():
                lines.append(raw)
except Exception:
    print(json.dumps({"ok": False, "error": "read-failed"})); sys.exit(0)

records = []  # (idx, obj, text, review, kind, grams, sort_key)
for idx, raw in enumerate(lines):
    try:
        obj = json.loads(raw)
    except Exception:
        obj = None
    if not isinstance(obj, dict):
        records.append((idx, None, None, None, None, None, None))
        continue
    review = str(obj.get("review") or "auto-local").lower()
    text = str(obj.get("text") or obj.get("summary") or "")
    kind = str(obj.get("kind") or "fact").lower()
    grams = ngrams(text)
    # Most-recent wins: prefer observed_at, then file order (later = newer).
    sort_key = (str(obj.get("observed_at") or ""), idx)
    records.append((idx, obj, text, review, kind, grams, sort_key))

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
superseded_idx = set()
cluster_count = 0
for members in clusters.values():
    if len(members) < 2:
        continue
    cluster_count += 1
    keeper = max(members, key=lambda r: r[6])  # newest by (observed_at, idx)
    for r in members:
        if r[0] == keeper[0]:
            continue
        if r[3] in SUPERSEDABLE:  # only auto-generated copies are demoted
            superseded_idx.add(r[0])

if not superseded_idx:
    print(json.dumps({"ok": True, "total": len(lines), "clusters": cluster_count,
                      "superseded": 0, "changed": False}))
    sys.exit(0)

out_lines = []
for idx, raw in enumerate(lines):
    if idx in superseded_idx:
        obj = by_idx[idx][1]
        obj = dict(obj)
        obj["review"] = "superseded"
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
                  "superseded": len(superseded_idx), "changed": True}))
PY
