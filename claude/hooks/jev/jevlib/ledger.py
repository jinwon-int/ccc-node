"""Decision ledger — append-only JSONL record of every Jev decision.

Schema ``jev.decision.v1`` (one JSON object per line):
  record_id      stable id (first 16 hex of sha256 over ts|state_hash|point|decision)
  ts             UTC ISO-8601
  domain         e.g. openmmo.sell | openmmo.dungeon | nunchi.judge
  session_id     game/ops session the decision belonged to
  decision_point semantic trigger (floor_cleared, hurt_checkpoint, sell_session)
  state_hash     sha256 of canonical JSON state (state itself kept for replay)
  state          sanitized game state (key/secret/token/password names stripped)
  primitive      jev question type (choice/score/...)
  question_id    key inside the questions object
  decision       jev's returned choice
  probability    jev calibrated probability for the choice
  confidence     jev confidence
  latency_ms     measured round trip
  model / prompt_version
  gate_action    observe | allow | deny | abort  (what the caller did with it)
  error          transport failure text (when the call failed)
  outcome        null until backfilled by the verification pass

Outcomes are what make this a calibration test bed: set_outcome() backfills a
record by id using an atomic tmp+rename rewrite under an flock.
"""

import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone

from .redact import redact_text

SCHEMA = "jev.decision.v1"
_REDACT_NAME = re.compile(r"key|secret|token|password", re.IGNORECASE)


def state_hash(state):
    canonical = json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def sanitize(obj):
    """Strip secrets recursively — by dict key name *and* by value shape.

    Key-name stripping alone is not enough: a consumer whose state is free text
    (vendor error prose, fetched page content, a log line) carries credentials
    inside string values, where no key name marks them.
    """
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if _REDACT_NAME.search(str(k)) else sanitize(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


class DecisionLedger:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        parent = os.path.dirname(self.path)
        os.makedirs(parent, exist_ok=True)
        if not os.path.exists(self.path):
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)

    def _lock(self, fh):
        fcntl.flock(fh, fcntl.LOCK_EX)

    def append(
        self,
        *,
        domain,
        session_id,
        decision_point,
        state,
        primitive,
        question_id,
        decision,
        probability=None,
        confidence=None,
        latency_ms=None,
        model=None,
        prompt_version=None,
        gate_action="observe",
        error=None,
    ):
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        sh = state_hash(state)
        record = {
            "schema": SCHEMA,
            "record_id": hashlib.sha256(
                f"{ts}|{sh}|{decision_point}|{decision}".encode()
            ).hexdigest()[:16],
            "ts": ts,
            "domain": domain,
            "session_id": session_id,
            "decision_point": decision_point,
            "state_hash": sh,
            "state": sanitize(state),
            "primitive": primitive,
            "question_id": question_id,
            "decision": decision,
            "probability": probability,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "model": model,
            "prompt_version": prompt_version,
            "gate_action": gate_action,
            "error": error,
            "outcome": None,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(self.path, "a") as fh:
            self._lock(fh)
            try:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return record["record_id"]

    def iter_records(self):
        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def set_outcome(self, record_id, outcome):
        """Backfill outcome for one record; returns True when the id matched."""
        records = list(self.iter_records())
        hit = False
        for rec in records:
            if rec.get("record_id") == record_id:
                rec["outcome"] = sanitize(outcome)
                hit = True
        if not hit:
            return False
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(self.path), prefix=".ledger-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as fh:
                for rec in records:
                    fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            # lock during swap so a concurrent append cannot be lost
            with open(self.path, "a") as lock_fh:
                self._lock(lock_fh)
                try:
                    os.replace(tmp, self.path)
                finally:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return hit

    def summary(self):
        by_domain, by_gate, outcomes = {}, {}, {"with": 0, "without": 0}
        n = 0
        for rec in self.iter_records():
            n += 1
            by_domain[rec["domain"]] = by_domain.get(rec["domain"], 0) + 1
            by_gate[rec["gate_action"]] = by_gate.get(rec["gate_action"], 0) + 1
            outcomes["with" if rec.get("outcome") is not None else "without"] += 1
        return {"records": n, "by_domain": by_domain, "by_gate_action": by_gate, "outcomes": outcomes}
