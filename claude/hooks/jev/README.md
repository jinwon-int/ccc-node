# jev — shared Jev (typesafe.ai System One) client + decision ledger

`jevlib` is the fleet's single transport and audit layer for Jev calls — the
typed-decision (Choice/Score) evaluation model. Consumers so far:

- nunchi judge cron (memory fact gating, `APPLY=1` + `MIN_CONFIDENCE` pattern)
- OpenMMO sell-session veto gate and dungeon live gate (jingun node, shadow
  proven at 99% hard-rule agreement on sells / 93.75% overall on dungeon
  decisions incl. 4/4 hurt checkpoints — reports under the node's
  `openmmo-jev-shadow/` evidence tree)

## Layout

- `jevlib/client.py` — key resolution (env → `TYPESAFE_API_KEY_FILE` →
  `~/.secrets/typesafe-api-key` → `~/.hermes/.env`), retrying POST, typed
  failures (`JevUnavailable` / `JevAPIError`). The key is never logged.
- `jevlib/ledger.py` — append-only JSONL decision ledger, schema
  `jev.decision.v1`: state hash, decision, calibrated probability/confidence,
  latency, gate action (`observe|allow|deny|abort`), and a backfillable
  `outcome` so calibration can be measured against real results.

## Gate rules encoded by callers (documented contract)

- **Shadow → APPLY promotion ladder**: replay evidence → measure agreement and
  calibration → only then wire a live gate with a `MIN_CONFIDENCE` threshold
  and a deterministic fallback on low confidence / transport failure.
- **Asymmetric veto**: Jev may only tighten (deny/abort), never loosen a
  deterministic safety policy. The OpenMMO sell gate can block a sell session;
  it can never force one. The dungeon gate can only end a run early.
- **Fail direction is per-gate policy**: irreversible actions (sell) fail
  closed on missing key/API failure; intrusive actions (retreat) fail open to
  observe so an API outage cannot abort a healthy run.

Secrets never enter the ledger: state payloads are sanitized recursively
(any dict key matching `key|secret|token|password` is replaced with
`<redacted>`), and files are created `0600`.

## Usage

```python
import sys
sys.path.insert(0, str(REPO_ROOT / "claude" / "hooks" / "jev"))

from jevlib import DecisionLedger, JevClient, choice_probability

client = JevClient()  # key resolved from env/key files
answer, meta = client.choice(
    state, "next_action",
    {"type": "choice", "instructions": "...", "criteria": {...}})
# answer = {"type": "choice", "choice": "descend",
#           "probabilities": {"descend": 0.8, "retreat": 0.2}, "confidence": 0.73}
# NOTE: `probabilities` is a map, not a scalar. Use choice_probability(answer)
# for the ledger's `probability` column — calibration is measured from it.

ledger = DecisionLedger("/var/lib/mygate/decisions.jsonl")
rid = ledger.append(domain="mygate", session_id=s, decision_point="x",
                    state=state, primitive="choice", question_id="next_action",
                    decision=answer["choice"], confidence=answer["confidence"],
                    probability=choice_probability(answer),
                    gate_action="observe")
# later, when the real-world result is known:
ledger.set_outcome(rid, {"survived": True})
```
