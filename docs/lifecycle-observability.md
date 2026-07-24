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
- **Live opt-in wiring** — `normalize_agent_event` maps live `AgentEvent`s
  (tool/turn/approval) for both providers; a `LifecycleObserver` (built by
  `build_lifecycle_observer`, gated by **`CCC_LIFECYCLE_AUDIT`**, default **off**)
  taps the bridge event consume loop and records to the ledger. The tap is
  fail-open and a no-op on a default node.
- **Claude Bash-hook feed** — the installed `audit.sh`, `redact.sh`, and
  `notify.sh` hooks pass their original stdin to `lifecycle-feed.sh`, which
  invokes the same `telegram_bot.core.lifecycle_hook` CLI. The bridge exports
  the validated gate and shared ledger path to hook subprocesses. The feed is
  default-off, fail-open, and supports an explicit `CCC_LIFECYCLE_PYTHON`
  interpreter override for non-standard installs.
- **Body-free legacy compatibility records** — the legacy audit/evidence
  helpers retain their existing opt-in behavior without persisting raw
  commands, paths, session IDs, or provider notification bodies. Session scope
  is an opaque hash and evidence is stored as booleans. Existing state parents
  must already be owner-only; new ones are created `0700`, files are `0600`,
  and symlink targets/parents are refused. Owner spool text is a fixed body-free
  notice; notification audit, approval, and spool records use canonical-JSON,
  payload-stable retry dedup. Session archives are published from a private temp
  file without overwriting an existing destination.
- Capability matrix: a `lifecycle_observability` axis (both providers remain
  `degraded`; Claude hook-payload feed is wired, while provider notification
  delivery and an official Codex checkpoint boundary remain follow-ups).

## Event mapping

| Lifecycle event | Claude source | Codex source |
|---|---|---|
| `prompt_submitted` | `UserPromptSubmit` hook | `turn/started` |
| `tool_completed` | `PostToolUse` hook (mutating only) | `item/completed` (tool item) |
| `turn_completed` | `Stop` hook | `turn/completed` |
| `session_closed` | `SessionEnd` hook | thread teardown (follow-up) |
| `provider_notification` | `Notification` hook | `*requestApproval` |

## Enabling (opt-in, canary)

Set `CCC_LIFECYCLE_AUDIT=true` on a node's bridge `.env` and restart. Live
tool/turn/approval `AgentEvent`s and installed Claude Bash hooks then record
body-free observations into
`<bot_data_dir>/lifecycle-audit/lifecycle-audit.jsonl` (owner-only, bounded,
deduped). Default off; the tap is fail-open and never blocks a turn.

## Scope / follow-ups (canary-gated)

- **Claude hook-payload parity**: `python3 -m telegram_bot.core.lifecycle_hook
  <event>` reads a Claude hook's stdin JSON, normalizes it, and records to the
  ledger (fail-open, exit 0, no-op unless `CCC_LIFECYCLE_AUDIT`). The shipped
  Bash hooks now feed it for hook-only events such as
  `prompt_submitted`/`session_closed`; enabling the audit gate remains the
  operator's opt-in step.
- **Evidence-gate detection landed** (body-free): each `tool_completed`
  observation carries `file_change` / `verification` booleans (computed from the
  command, which is never stored), and `evidence_gate(observations)` returns a
  provider-neutral verdict — a turn that changed files but ran no verification
  action needs evidence. When the opt-in observer is on, it tallies evidence per
  turn and, on turn completion, **records a body-free `evidence-missing` warning
  observation** to the ledger — it only appends a record, never blocks the turn
  or re-prompts, so there is no stop loop. With `CCC_LIFECYCLE_AUDIT_NOTIFY=true`
  (a second opt-in) it also writes a body-free owner notice to the push spool;
  the actual Telegram send stays gated by the push notifier
  (`CCC_PUSH_ENABLED`) — no direct provider send. `Notification`/`PreCompact`
  checkpoint parity remain follow-ups.
- **Redaction residuals**: `skill_candidate` and `distill_extraction` import the
  canonical set from `bridge/utils/redaction.py`. Bash lifecycle persistence is
  now body-free instead of maintaining another replacement regex. `agent_cron`
  still has a broader owner-spool redaction set and remains a follow-up.
- **No synthetic compaction**: Codex exposes no official `PreCompact` event in
  the current runtime contract, so this slice does not invent one or promote
  the capability. Checkpoint parity remains degraded until an official
  provider boundary can be observed.
- Autonomous-write rollback/kill-switch is **#386**; Codex memory write-back is
  **#465** — out of scope here.
