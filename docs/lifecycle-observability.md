# Lifecycle observability (provider-neutral) — #645

Claude native hooks and the Codex app-server surface different event shapes but
carry the same operational signal (a tool ran, a turn finished, a credential
appeared in a prompt, a notification fired). This layer converges both onto one
versioned, **body-free** contract so audit/redaction/evidence/notification
parity does not depend on Claude-only hook payloads.

## What landed (this slice, source/test/docs only)

- **`bridge/utils/redaction.py`** — the single canonical credential pattern set
  (promoted from the memory-distill extractor; covers bearer, Telegram bot
  token, `gh*_`/`github_pat_`, `sk-`, `AKIA`, credential assignments, and full
  `BEGIN…END PRIVATE KEY` blocks). `contains_credential` (warn) and
  `redact_credentials` (substitute → `[REDACTED_CREDENTIAL]`). Prefer importing
  this over per-module copies.
- **`bridge/core/lifecycle_observation.py`** — `LifecycleObservation` (versioned
  schema) with five event types: `prompt_submitted`, `tool_completed`,
  `turn_completed`, `session_closed`, `provider_notification`. Normalizers for
  **raw Claude hook payloads** (`normalize_claude_hook`) and **raw Codex
  app-server notifications** (`normalize_codex_app_server`). Correlation ids are
  opaque salted hashes; tool targets reduce to a shape (`file`/`command`); a
  credential in a prompt becomes a flag, never a stored value. Read-only tools
  and malformed/unknown/non-tool events produce no observation.
- **`bridge/core/lifecycle_audit.py`** — an owner-only (`0700`/`0600`), atomic,
  **bounded** (newest-N + per-record byte cap), **deduped** (by `dedup_key`),
  **fail-open** audit ledger. A write failure returns a body-free status and
  never raises into a turn path.
- Capability matrix: a `lifecycle_observability` axis (both providers
  `degraded` — contract landed, live wiring pending).

## Event mapping

| Lifecycle event | Claude source | Codex source |
|---|---|---|
| `prompt_submitted` | `UserPromptSubmit` hook | `turn/started` |
| `tool_completed` | `PostToolUse` hook (mutating only) | `item/completed` (tool item) |
| `turn_completed` | `Stop` hook | `turn/completed` |
| `session_closed` | `SessionEnd` hook | thread teardown (follow-up) |
| `provider_notification` | `Notification` hook | `*requestApproval` |

## Scope / follow-ups (canary-gated)

- **Live wiring**: feeding real Claude hook stdin and Codex `AgentEvent`s into
  the normalizers and the audit ledger from the bridge runtime.
- **Evidence gate + notification/checkpoint parity** on the Codex path
  (criteria for `Stop`/`Notification`/`PreCompact` equivalents).
- **Redaction unification**: migrate the remaining per-module credential
  pattern copies (`distill_extraction`, `skill_candidate`, `agent_cron`,
  `audit.sh`) onto `bridge/utils/redaction.py`.
- Autonomous-write rollback/kill-switch is **#386**; Codex memory write-back is
  **#465** — out of scope here.
