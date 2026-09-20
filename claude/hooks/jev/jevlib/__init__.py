"""jevlib — shared Jev (typesafe.ai System One) client + decision ledger.

Extracted from the OpenMMO jev-shadow scripts so the nunchi judge, the OpenMMO
gates, and future fleet consumers share one transport, one key-resolution
order, and one decision-ledger schema.

Rules encoded here (do not bypass in callers):
  - Raw secrets never appear in logs, errors, or ledger records.
  - Game/ops state is safe to persist; anything whose *name* matches
    key/secret/token/password is stripped at any depth before writing, and
    every string *value* is additionally masked for secret shapes — free-text
    state (error prose, fetched content) carries credentials that no key name
    marks.
  - API failures surface as typed exceptions; callers decide fail-open vs
    fail-closed per gate policy.
"""

from .client import (
    DEFAULT_API_URL,
    DEFAULT_MODEL,
    RETRYABLE_4XX,
    choice_probability,
    JevAPIError,
    JevClient,
    JevUnavailable,
    resolve_key,
)
from .ledger import SCHEMA as LEDGER_SCHEMA
from .ledger import DecisionLedger, state_hash
from .redact import redact_text

__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_MODEL",
    "RETRYABLE_4XX",
    "choice_probability",
    "JevAPIError",
    "JevClient",
    "JevUnavailable",
    "resolve_key",
    "LEDGER_SCHEMA",
    "DecisionLedger",
    "state_hash",
    "redact_text",
]
