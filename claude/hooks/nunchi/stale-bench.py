#!/usr/bin/env python3
"""STALE implicit-conflict regression bench for nunchi (ccc-node#2464). LLM-free, deterministic.

Phase A (isolated): per scenario a fresh DB ingests M_old then M_new under the user peer and
reports whether the write gates reacted — `closed` (M_old got valid_to via G1/B2), `flagged`
(M_new review=1 via G3) or `missed`. Phase B (pooled): every scenario's M_old then M_new in one
DB, then `search()` with the dim1 query; reports M_new/M_old recall@5/@10, the stale-trap rate
(only M_old in top-5) and cross-scenario gate side effects (false closes, G3 flags). Phase R
(recent-state): per scenario M_old + 40 older distractors + M_new last — the shape a real peer
timeline has — reports whether both states reach the top-10 of the production recall path.

The node's real DB is never opened: NUNCHI_DB is pointed at a private temp dir before nunchi.py
is imported. Exit 2 when a metric regresses past `stale-baseline.json` (a ratchet, never a
target): detection may only go up, false closes only down.
"""
from __future__ import annotations
import argparse
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
HERE = os.path.dirname(os.path.abspath(__file__))

def _load_nunchi(db_path: str):
    os.environ["NUNCHI_DB"] = db_path
    spec = importlib.util.spec_from_file_location("nunchi", os.path.join(HERE, "nunchi.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod

def _use_db(nunchi, path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path + suffix)
    nunchi.DB = path
    os.environ["NUNCHI_DB"] = path

def _ingest(nunchi, tmp: str, sid: str, text: str) -> str:
    payload = os.path.join(tmp, f"{sid}.json")
    with open(payload, "w", encoding="utf-8") as fh:
        json.dump({"session_id": sid, "honcho": [{"kind": "fact", "subject": "user", "text": text}]}, fh)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            nunchi.ingest(payload)
    except SystemExit as exc:  # ingest fails closed on bad payloads; report, never crash the bench
        return f"exit:{exc}"
    return out.getvalue().strip()

def _rows(path: str) -> dict[str, tuple]:
    con = sqlite3.connect(path)
    try:
        return {r[1]: r for r in con.execute("SELECT id,fact,review,valid_to,supersedes FROM peer_facts")}
    finally:
        con.close()

def phase_a(nunchi, scenarios: list[dict], tmp: str) -> dict:
    counts = {"closed": 0, "flagged": 0, "missed": 0, "not_stored": 0}
    for i, s in enumerate(scenarios):
        path = os.path.join(tmp, f"iso-{i}.db")
        _use_db(nunchi, path)
        _ingest(nunchi, tmp, f"old-{i}", s["M_old"])
        _ingest(nunchi, tmp, f"new-{i}", s["M_new"])
        rows = _rows(path)
        old, new = rows.get(s["M_old"].strip()), rows.get(s["M_new"].strip())
        if old is None or new is None:
            counts["not_stored"] += 1
        elif old[3]:
            counts["closed"] += 1
        elif new[2]:
            counts["flagged"] += 1
        else:
            counts["missed"] += 1
    counts["detected"] = counts["closed"] + counts["flagged"]
    return counts

def _search(nunchi, con, query: str, *, state_aware: bool):
    """Production recall path (state_aware) or plain bm25; older nunchi.py lacks the flag."""
    try:
        return nunchi.search(con, query, nunchi.USER_PEER, limit=10, include_history=True, state_aware=state_aware)
    except TypeError:
        return nunchi.search(con, query, nunchi.USER_PEER, limit=10, include_history=True)


def _has_vectors(path: str) -> bool:
    con = sqlite3.connect(path)
    try:
        return bool(con.execute("SELECT 1 FROM fact_vecs LIMIT 1").fetchone())
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def phase_b(nunchi, scenarios: list[dict], tmp: str) -> dict:
    path = os.path.join(tmp, "pooled.db")
    _use_db(nunchi, path)
    for i, s in enumerate(scenarios):
        _ingest(nunchi, tmp, f"old-{i}", s["M_old"])
    for i, s in enumerate(scenarios):
        _ingest(nunchi, tmp, f"new-{i}", s["M_new"])
    rows = _rows(path)
    con = sqlite3.connect(path)
    m = {"n": len(scenarios), "new_at5": 0, "new_at10": 0, "old_at5": 0, "stale_trap_at5": 0,
         "lex_new_at5": 0, "lex_new_at10": 0, "vectors": _has_vectors(path),
         "old_closed": 0, "new_flagged": 0, "not_stored": 0}
    try:
        for s in scenarios:
            old, new = rows.get(s["M_old"].strip()), rows.get(s["M_new"].strip())
            if old is None or new is None:
                m["not_stored"] += 1
                continue
            ids = [h[0] for h in _search(nunchi, con, s["dim1_query"], state_aware=True)]
            lex_ids = [h[0] for h in _search(nunchi, con, s["dim1_query"], state_aware=False)]
            new_rank = ids.index(new[0]) + 1 if new[0] in ids else None
            old_rank = ids.index(old[0]) + 1 if old[0] in ids else None
            m["lex_new_at5"] += bool(new[0] in lex_ids[:5])
            m["lex_new_at10"] += bool(new[0] in lex_ids)
            new5 = bool(new_rank and new_rank <= 5)
            old5 = bool(old_rank and old_rank <= 5)
            m["new_at5"] += new5
            m["new_at10"] += bool(new_rank)
            m["old_at5"] += old5
            m["stale_trap_at5"] += old5 and not new5
            m["old_closed"] += bool(old[3])
            m["new_flagged"] += bool(new[2])
    finally:
        con.close()
    return m

def phase_r(nunchi, scenarios: list[dict], tmp: str, distractors: int = 40) -> dict:
    """Recent-state shape: per scenario a fresh DB holds M_old, then `distractors` other
    scenarios' M_old facts (older noise), then M_new last. The dim1 query is asked with the
    production recall path. Measures whether the latest state about the peer is visible at all
    (window/recency) when the lexical query only names the old state."""
    m = {"n": len(scenarios), "distractors": distractors, "new_at10": 0, "old_at10": 0, "both_at10": 0, "lex_both_at10": 0, "not_stored": 0}
    for i, s in enumerate(scenarios):
        path = os.path.join(tmp, f"recent-{i}.db")
        _use_db(nunchi, path)
        _ingest(nunchi, tmp, f"rold-{i}", s["M_old"])
        for k in range(1, distractors + 1):
            d = scenarios[(i + k) % len(scenarios)]
            _ingest(nunchi, tmp, f"rdis-{i}-{k}", d["M_old"])
        _ingest(nunchi, tmp, f"rnew-{i}", s["M_new"])
        rows = _rows(path)
        old, new = rows.get(s["M_old"].strip()), rows.get(s["M_new"].strip())
        if old is None or new is None:
            m["not_stored"] += 1
            continue
        con = sqlite3.connect(path)
        try:
            ids = [h[0] for h in _search(nunchi, con, s["dim1_query"], state_aware=True)]
            lex_ids = [h[0] for h in _search(nunchi, con, s["dim1_query"], state_aware=False)]
        finally:
            con.close()
        n10, o10 = new[0] in ids, old[0] in ids
        m["new_at10"] += n10
        m["old_at10"] += o10
        m["both_at10"] += n10 and o10
        m["lex_both_at10"] += (new[0] in lex_ids) and (old[0] in lex_ids)
    return m


def _regressions(a: dict, b: dict, base: dict) -> list[str]:
    """Only keys present in the baseline ratchet; an empty baseline only checks storage."""
    checks = (
        ("a_detected_min", a["detected"], "<", "A detected"),
        ("b_new_at5_min", b["new_at5"], "<", "B M_new@5"),
        ("b_old_closed_max", b["old_closed"], ">", "B false closes"),
        ("b_new_flagged_max", b["new_flagged"], ">", "B G3 flags"),
    )
    out = []
    for key, value, op, label in checks:
        if key not in base:
            continue
        limit = base[key]
        if (op == "<" and value < limit) or (op == ">" and value > limit):
            out.append(f"{label} {value} {op} baseline {limit}")
    if a["not_stored"] or b["not_stored"]:
        out.append(f"scenarios not stored: A {a['not_stored']} B {b['not_stored']}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixture", default=os.path.join(HERE, "stale-fixture.json"))
    ap.add_argument("--baseline", default=os.path.join(HERE, "stale-baseline.json"))
    ap.add_argument("--json", action="store_true", help="print the metrics as one JSON object")
    args = ap.parse_args(argv)
    with open(args.fixture, encoding="utf-8") as fh:
        scenarios = json.load(fh)["scenarios"]
    base = {}
    if os.path.exists(args.baseline):
        with open(args.baseline, encoding="utf-8") as fh:
            raw = fh.read().strip()
        base = json.loads(raw) if raw else {}
    with tempfile.TemporaryDirectory(prefix="nunchi-stale-") as tmp:
        os.chmod(tmp, 0o700)
        nunchi = _load_nunchi(os.path.join(tmp, "import.db"))
        a = phase_a(nunchi, scenarios, tmp)
        b = phase_b(nunchi, scenarios, tmp)
        r = phase_r(nunchi, scenarios, tmp)
    regressions = _regressions(a, b, base)
    if "r_both_at10_min" in base and r["both_at10"] < base["r_both_at10_min"]:
        regressions.append(f"R both@10 {r['both_at10']} < baseline {base['r_both_at10_min']}")
    result = {"scenarios": len(scenarios), "phase_a": a, "phase_b": b, "phase_r": r, "baseline": base, "regressions": regressions}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        n = len(scenarios)
        print(f"stale-bench: n={n} A detected={a['detected']} (closed {a['closed']}, flagged {a['flagged']}) missed={a['missed']}")
        print(f"stale-bench: B M_new@5={b['new_at5']}/{n} M_new@10={b['new_at10']}/{n} M_old@5={b['old_at5']}/{n} "
              f"stale_trap@5={b['stale_trap_at5']}/{n} false_closes={b['old_closed']} g3_flags={b['new_flagged']} "
              f"(plain bm25: M_new@5={b['lex_new_at5']}/{n} @10={b['lex_new_at10']}/{n}; vectors={b['vectors']})")
        print(f"stale-bench: R(recent-state, {r['distractors']} distractors) M_new@10={r['new_at10']}/{n} "
              f"M_old@10={r['old_at10']}/{n} both@10={r['both_at10']}/{n} (plain bm25 both@10={r['lex_both_at10']}/{n})")
        for reg in regressions:
            print(f"REGRESSION: {reg}")
    return 2 if regressions else 0

if __name__ == "__main__":
    sys.exit(main())
