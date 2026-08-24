#!/usr/bin/env python3
"""nunchi judge-batch — daily review-queue triage (#1204, TM-2370 P0-c).

Deterministic-first design (owner-approved 2026-08-21): the gwakga pilot
classified 8/9 queued items as "G2 demotion, no sibling conflict" — a class
an LLM is not needed for. So the batch FIRST re-runs the write gate's own
sibling-conflict rule (G3, _conflict_review: same-observed open sibling with
>= 0.6 token overlap) at batch time. An item with no live conflicting sibling
is cleared without any LLM call; only items with a live conflict go to the
judge (`claude -p` haiku, strict rubric). The semantic contract is nunchi.py's
write gate itself — this script imports it instead of copying the rule, so the
two can never drift.

Guard rails (issue #1204 contract):
- daily cadence via install-nunchi.sh cron (managed marker; an unmanaged cron
  trips doctor cron-drift), flock against concurrent runs
- CAP items per run (default 10, oldest first)
- items younger than MIN_AGE_HOURS (default 24) are inviolable
- the only automatic mutation is `review=0` (the `review <id> --clear`
  equivalent); supersede appears in the report as proposal text only
- G5 (#1264): a reasonless decision is never deterministic-cleared — it has
  no live sibling by construction, so the deterministic pass would hide the
  missing reason. Class g5-reasonless-decision, always verdict=human, and
  the audit points the owner at `annotate <id> --because`.
- judge failure / unparseable verdict is fail-closed (human-approval)
- NUNCHI_JUDGE_APPLY=1 to mutate; default is dry-run
- before an apply run mutates: DB backup to ~/.nunchi/backup/; per-item
  mutation-time recheck (still open + still flagged); append-only audit log
  ~/.nunchi/judge-audit.jsonl
- report ~/.claude/state/nunchi-review-report.md + flag file when human items
  remain (local notification path; bridge send is an open question in #1204)

Audience-scoped nodes: like bench.sh, the parent pass enumerates canonical
scope children of CCC_NUNCHI_AUDIENCE_ROOT (shared / private-[0-9a-f]{32},
owned, non-group-accessible) and re-runs per scope that has a facts.db.
"""

import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Semantic contract: the deterministic pass must mean exactly what the write
# gate means. Import the module (main-guarded, lazy db) rather than copying
# _tokens/_conflict_review — a copied rule would drift (#1204 design note,
# mirrored from the "두 레인이 다른 의미를 쓰면 이후 감사가 오염" lesson of #1211).
import nunchi  # noqa: E402

DB = os.environ.get("NUNCHI_DB", os.path.expanduser("~/.nunchi/facts.db"))
HOME_DIR = os.environ.get("NUNCHI_HOME", os.path.expanduser("~/.nunchi"))
STATE = os.environ.get("CCC_STATE_DIR", os.path.expanduser("~/.claude/state"))
APPLY = os.environ.get("NUNCHI_JUDGE_APPLY") == "1"
JUDGE_CMD = os.environ.get("NUNCHI_JUDGE_CMD", "claude")
JUDGE_MODEL = os.environ.get("NUNCHI_JUDGE_MODEL", "haiku")
AUDIT = os.path.join(HOME_DIR, "judge-audit.jsonl")
REPORT = os.path.join(STATE, "nunchi-review-report.md")
FLAG = os.path.join(STATE, "nunchi-judge-human.flag")
LOCK = os.path.join(HOME_DIR, ".judge.lock")
BACKUP_DIR = os.path.join(HOME_DIR, "backup")


def _int_env(name, default, low, high):
    try:
        val = int(os.environ.get(name, str(default)))
    except ValueError:
        val = default
    return max(low, min(high, val))


CAP = _int_env("NUNCHI_JUDGE_CAP", 10, 1, 50)
MIN_AGE_HOURS = _int_env("NUNCHI_JUDGE_MIN_AGE_HOURS", 24, 1, 24 * 30)
JUDGE_TIMEOUT = _int_env("NUNCHI_JUDGE_TIMEOUT_SEC", 120, 10, 600)
MAX_SCOPES = _int_env("CCC_NUNCHI_MAX_SCOPES_PER_RUN", 64, 1, 64)

VERDICTS = ("clear", "conflict", "human")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Audience-scope fan-out (same contract as bench.sh)
# ---------------------------------------------------------------------------

def canonical_scope_children(root, limit):
    """Canonical direct children of the opaque audience root.

    Mirrors bench.sh's enumerator (which is byte-identical across bench.sh /
    piri-feed.sh / mempalace-refresh.sh): root and each child must be a
    directory owned by us with no group/other access; names are 'shared' or
    'private-<32 lowercase hex>'; sorted, capped. Anything else is skipped.
    """
    try:
        meta = os.lstat(root)
    except OSError:
        return []
    import stat as _stat
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
    """Re-run this script per canonical scope that has a fact store."""
    root = os.environ.get("CCC_NUNCHI_AUDIENCE_ROOT", "")
    rc = 0
    for scope_root in canonical_scope_children(root, MAX_SCOPES):
        scope_db = os.path.join(scope_root, "nunchi", "facts.db")
        if not os.path.isfile(scope_db):
            continue
        scope = os.path.basename(scope_root)
        env = dict(os.environ)
        env["CCC_NUNCHI_SCOPED_CHILD"] = "1"
        env["CCC_NUNCHI_AUDIENCE_SCOPE"] = scope
        env["CCC_NUNCHI_AUDIENCE_KIND"] = "shared" if scope == "shared" else "private"
        env["NUNCHI_HOME"] = os.path.join(scope_root, "nunchi")
        env["NUNCHI_DB"] = scope_db
        env["NUNCHI_SNAPSHOT"] = os.path.join(scope_root, "nunchi", "snapshot.md")
        proc = subprocess.run([sys.executable, os.path.abspath(__file__)], env=env)
        rc = rc or proc.returncode
    return rc


# ---------------------------------------------------------------------------
# Queue + deterministic pass
# ---------------------------------------------------------------------------

def fetch_queue(conn):
    """Oldest-first flagged facts older than the freshness moat."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MIN_AGE_HOURS)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT id, observed, kind, fact, source_rank, created_at, because FROM peer_facts"
        " WHERE valid_to IS NULL AND review=1 AND created_at <= ?"
        " ORDER BY id LIMIT ?",
        (cutoff, CAP),
    ).fetchall()


def live_conflict(conn, fact_id, observed, text):
    """The write gate's G3 rule re-run at batch time, excluding the item itself.

    At ingest _conflict_review runs before the newcomer is inserted, so it
    never self-matches; the batch recheck must exclude the queued item's own
    row explicitly. Tokenization and the 0.6 threshold come from nunchi.py
    verbatim (imported, not copied).
    """
    new = nunchi._tokens(text)
    if not new:
        return []
    hits = []
    for fid, fact in conn.execute(
            "SELECT id, fact FROM peer_facts"
            " WHERE observed=? AND valid_to IS NULL AND id != ?",
            (observed, fact_id)).fetchall():
        old = nunchi._tokens(fact)
        if old and len(new & old) / min(len(new), len(old)) >= 0.6:
            hits.append((fid, fact))
    return hits


# ---------------------------------------------------------------------------
# Judge (remainder only)
# ---------------------------------------------------------------------------

JUDGE_SYSTEM = (
    "You triage one flagged fact in a personal memory store. The fact was "
    "flagged because it has high token overlap with an existing open fact — a "
    "possible contradiction or drifted duplicate. Answer with exactly one JSON "
    "object and nothing else."
)


def build_judge_prompt(item, siblings):
    fid, observed, kind, text, rank, _created, _because = item
    sib_lines = "\n".join(f"- #{sid}: {sfact}" for sid, sfact in siblings[:5])
    return f"""Flagged fact #{fid} (kind={kind}, observed={observed}, source_rank={rank}):
{text}

Open sibling fact(s) with high overlap:
{sib_lines}

Decide one verdict:
- "clear": the flagged fact is a duplicate or restatement, or the flag is stale. Both facts stay in the store; only the review flag is cleared.
- "conflict": the facts genuinely contradict and a human must resolve. Add a one-line supersede proposal naming which fact should win and why.
- "human": anything ambiguous or unsafe to decide.

Answer with exactly one JSON object:
{{"verdict":"clear|conflict|human","rationale":"<=200 chars","supersede_proposal":null|"<=200 chars"}}"""


def judge_available():
    return shutil.which(JUDGE_CMD) is not None


def judge_item(item, siblings):
    """One hardened `claude -p` call; any failure is fail-closed (human)."""
    argv = [
        JUDGE_CMD, "-p",
        "--tools", "",
        "--disallowedTools", "mcp__*",
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
        "--model", JUDGE_MODEL,
        "--no-session-persistence",
        "--output-format", "text",
        "--append-system-prompt", JUDGE_SYSTEM,
    ]
    try:
        proc = subprocess.run(
            argv,
            input=build_judge_prompt(item, siblings),
            capture_output=True,
            text=True,
            timeout=JUDGE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"verdict": "human", "rationale": "judge invocation failed", "supersede_proposal": None}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"verdict": "human", "rationale": f"judge exited {proc.returncode} or empty", "supersede_proposal": None}
    match = re.search(r"\{.*\}", proc.stdout, re.DOTALL)
    if not match:
        return {"verdict": "human", "rationale": "judge output had no JSON object", "supersede_proposal": None}
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return {"verdict": "human", "rationale": "judge JSON unparseable", "supersede_proposal": None}
    verdict = parsed.get("verdict")
    if verdict not in VERDICTS:
        return {"verdict": "human", "rationale": "judge verdict outside rubric", "supersede_proposal": None}
    return {
        "verdict": verdict,
        "rationale": str(parsed.get("rationale") or "")[:200],
        "supersede_proposal": (None if parsed.get("supersede_proposal") in (None, "")
                               else str(parsed.get("supersede_proposal"))[:200]),
    }


# ---------------------------------------------------------------------------
# Apply + audit + report
# ---------------------------------------------------------------------------

def audit(entry):
    os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
    with open(AUDIT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def backup_db():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"facts-prejudge-{stamp}.db")
    shutil.copy2(DB, dest)
    return dest


def apply_clear(conn, fact_id):
    """Mutation-time recheck, then the single allowed mutation (review=0)."""
    row = conn.execute(
        "SELECT review, valid_to FROM peer_facts WHERE id=?", (fact_id,)).fetchone()
    if not row or row[0] != 1 or row[1] is not None:
        return False
    conn.execute("UPDATE peer_facts SET review=0 WHERE id=?", (fact_id,))
    return True


def write_report(payload, human_items):
    os.makedirs(STATE, exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(payload)
    if human_items:
        with open(FLAG, "w", encoding="utf-8") as fh:
            fh.write(f"{len(human_items)} human-pending item(s) as of {now()}\n")
    elif os.path.exists(FLAG):
        os.unlink(FLAG)


def triage_queue(conn, queue):
    """Deterministic pass first; only live-conflict items spend a judge call.

    G5 (#1264) precedes both: a reasonless decision has no live sibling by
    construction, so the deterministic rule would clear it and silently hide
    the missing reason. That gap is owner-actionable (annotate), never
    batch-clearable.
    """
    decisions = []
    for item in queue:
        fid, observed, kind, text, rank, created, because = item
        if nunchi._g5_reasonless_decision(kind, text, because):
            decisions.append({
                "id": fid, "class": "g5-reasonless-decision",
                "rationale": ("decision without its reason (G5, #1264) — owner backfill: "
                              f"nunchi.py annotate {fid} --because <reason>; "
                              "clearing would hide the gap"),
                "verdict": "human", "supersede_proposal": None,
            })
            continue
        siblings = live_conflict(conn, fid, observed, text)
        if not siblings:
            decisions.append({
                "id": fid, "class": "deterministic-clear",
                "rationale": "no live >=0.6-overlap open sibling at batch time (write-gate rule re-run)",
                "verdict": "clear", "supersede_proposal": None,
            })
        elif not judge_available():
            decisions.append({
                "id": fid, "class": "judge-unavailable",
                "rationale": f"{JUDGE_CMD} not on PATH — fail-closed to human",
                "verdict": "human", "supersede_proposal": None,
            })
        else:
            verdict = judge_item(item, siblings)
            decisions.append({
                "id": fid, "class": "judge",
                "rationale": verdict["rationale"],
                "verdict": verdict["verdict"],
                "supersede_proposal": verdict["supersede_proposal"],
            })
    return decisions


def apply_decisions(conn, decisions):
    """Backup once, then per-item mutation-time recheck + the single mutation."""
    clears = [d for d in decisions if d["verdict"] == "clear"]
    applied = 0
    backup = ""
    if APPLY and clears:
        backup = backup_db()
        for d in clears:
            if apply_clear(conn, d["id"]):
                applied += 1
                d["applied"] = True
            else:
                d["applied"] = False
                d["class"] = "skipped-stale"
        conn.commit()
    return clears, applied, backup


def build_report(stamp, decisions, clears, humans, applied, backup):
    mode = "APPLY" if APPLY else "dry-run"
    lines = [
        f"# nunchi judge-batch report — {stamp}",
        "",
        f"- mode: **{mode}** (NUNCHI_JUDGE_APPLY={'1' if APPLY else 'unset'})",
        f"- db: `{DB}`",
        f"- queue processed: {len(decisions)} (CAP {CAP}, freshness moat {MIN_AGE_HOURS}h)",
        f"- deterministic clear: {sum(1 for d in decisions if d['class'] == 'deterministic-clear')}",
        f"- judge: {sum(1 for d in decisions if d['class'] == 'judge')}"
        f" (clear {sum(1 for d in decisions if d['class'] == 'judge' and d['verdict'] == 'clear')})",
        f"- human-pending: {len(humans)}"
        + (f" (judge unavailable: {sum(1 for d in decisions if d['class'] == 'judge-unavailable')})"
           if any(d["class"] == "judge-unavailable" for d in decisions) else ""),
    ]
    if APPLY:
        lines.append(f"- applied clears: {applied}" + (f" · backup `{backup}`" if backup else ""))
    if decisions:
        lines += ["", "| id | class | verdict | rationale |", "|---|---|---|---|"]
        for d in decisions:
            rationale = d["rationale"].replace("|", "\\|")
            lines.append(f"| #{d['id']} | {d['class']} | {d['verdict']} | {rationale} |")
    if humans:
        lines += ["", "## human-pending", ""]
        for d in humans:
            lines.append(f"- #{d['id']} ({d['class']}): {d['rationale']}")
            if d["supersede_proposal"]:
                lines.append(f"  - supersede proposal (apply manually): {d['supersede_proposal']}")
    return "\n".join(lines) + "\n"


def run_single_db():
    if not os.path.isfile(DB):
        print(f"judge-batch: no fact store at {DB} — nothing to do")
        return 0
    os.makedirs(HOME_DIR, exist_ok=True)
    with open(LOCK, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("judge-batch: another run holds the lock — skipping")
            return 0
        conn = sqlite3.connect(DB)
        decisions = triage_queue(conn, fetch_queue(conn))
        clears, applied, backup = apply_decisions(conn, decisions)
        humans = [d for d in decisions if d["verdict"] != "clear"]
        stamp = now()
        for d in decisions:
            audit({
                "ts": stamp, "db": DB, "id": d["id"], "class": d["class"],
                "verdict": d["verdict"], "applied": bool(APPLY and d.get("applied")),
                "rationale": d["rationale"], "supersede_proposal": d["supersede_proposal"],
            })
        write_report(build_report(stamp, decisions, clears, humans, applied, backup), humans)
        conn.close()
        mode = "APPLY" if APPLY else "dry-run"
        print(f"judge-batch ({mode}): {len(decisions)} triaged,"
              f" {len(clears)} clear, {len(humans)} human-pending"
              + (f", {applied} applied" if APPLY else ""))
        return 0


def main():
    if (os.environ.get("CCC_NUNCHI_AUDIENCE_SCOPED") == "1"
            and os.environ.get("CCC_NUNCHI_SCOPED_CHILD") != "1"):
        return fan_out_scopes()
    return run_single_db()


if __name__ == "__main__":
    sys.exit(main())
